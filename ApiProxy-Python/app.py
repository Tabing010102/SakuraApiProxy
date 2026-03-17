import argparse
import json
import logging
import threading
import multiprocessing
from itertools import cycle
from urllib.parse import urlparse

import opencc
import requests
from flask import Flask, request
from requests.adapters import HTTPAdapter

parser = argparse.ArgumentParser()
parser.add_argument('-l', '--listen_host', default='127.0.0.1', help='Host to listen on')
parser.add_argument('-p', '--listen_port', default=8081, type=int, help='Port to listen on')
parser.add_argument('-c', '--config', default='config.json', help='Config file')
parser.add_argument('-d', '--debug', action='store_true', help='Enable debug mode')
parser.add_argument(
    '--trust-env',
    action=argparse.BooleanOptionalAction,
    default=False,
    help='Trust environment and system proxy settings for upstream requests',
)
args = parser.parse_args()

if args.debug:
    logging.basicConfig(level=logging.DEBUG)

with open(args.config, 'r') as f:
    config = json.load(f)


def get_requests_session(prefix: str, max_concurrency: int, trust_env: bool) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env
    if max_concurrency >= 1:
        adapter = HTTPAdapter(pool_connections=max_concurrency, pool_maxsize=max_concurrency)
        session.mount(prefix, adapter)
    return session


endpoints = [{'endpoint': c['endpoint'],
              'semaphore': threading.Semaphore(c['max_concurrency']),
              'timeout': c['timeout'],
              'session': get_requests_session(
                  urlparse(c['endpoint']).scheme + '://',
                  c['max_concurrency'],
                  args.trust_env,
              )}
             for c in config['endpoints']]
endpoints_cycle = cycle(endpoints)

opencc_enabled = bool(config['enable_opencc'])
opencc_converter: opencc.OpenCC
if opencc_enabled:
    opencc_converter = opencc.OpenCC(config['opencc_config'])

app = Flask(__name__)

HOP_BY_HOP_RESPONSE_HEADERS = {
    'connection',
    'content-length',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'proxy-connection',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


def is_json_content_type(content_type: str) -> bool:
    mime_type = content_type.split(';', 1)[0].strip().lower()
    return mime_type == 'application/json' or mime_type.endswith('+json')


def is_event_stream_content_type(content_type: str) -> bool:
    return content_type.split(';', 1)[0].strip().lower() == 'text/event-stream'


def convert_response_json(response_json: dict) -> dict:
    for choice in response_json.get('choices', []):
        message = choice.get('message')
        if isinstance(message, dict) and isinstance(message.get('content'), str):
            message['content'] = opencc_converter.convert(message['content'])

        delta = choice.get('delta')
        if isinstance(delta, dict) and isinstance(delta.get('content'), str):
            delta['content'] = opencc_converter.convert(delta['content'])

        if isinstance(choice.get('text'), str):
            choice['text'] = opencc_converter.convert(choice['text'])

    if isinstance(response_json.get('content'), str):
        response_json['content'] = opencc_converter.convert(response_json['content'])

    return response_json


def convert_event_stream_response(response_text: str, json_ensure_ascii: bool) -> str:
    converted_lines = []
    for line in response_text.splitlines(keepends=True):
        stripped_line = line.rstrip('\r\n')
        line_ending = line[len(stripped_line):]

        if stripped_line.startswith('data: '):
            payload = stripped_line[6:]
            if payload and payload != '[DONE]':
                try:
                    payload_json = convert_response_json(json.loads(payload))
                    payload = json.dumps(payload_json, ensure_ascii=json_ensure_ascii, separators=(',', ':'))
                    line = f'data: {payload}{line_ending}'
                except json.JSONDecodeError:
                    pass

        converted_lines.append(line)

    return ''.join(converted_lines)


def get_response_headers(response: requests.Response):
    return [(key, value) for key, value in response.headers.items() if key.lower() not in HOP_BY_HOP_RESPONSE_HEADERS]


def get_body_preview(body: bytes, limit: int = 300) -> str:
    preview = body[:limit].decode('utf-8', errors='replace')
    if len(body) > limit:
        return f'{preview}...'
    return preview


def get_stream_flag(request_body: bytes):
    try:
        payload = json.loads(request_body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if isinstance(payload, dict):
        return payload.get('stream')

    return None


def get_next_available_endpoint():
    for endpoint in endpoints_cycle:
        if endpoint['semaphore'].acquire(blocking=False):
            return endpoint
    return None


def forward_request(request, endpoint):
    request_body = request.get_data()
    response = None
    try:
        app.logger.debug(
            'Forwarding request method=%s path=%s upstream=%s stream=%s body_preview=%r',
            request.method,
            request.path,
            endpoint['endpoint'],
            get_stream_flag(request_body),
            get_body_preview(request_body),
        )

        response = endpoint['session'].request(
            method=request.method,
            url=endpoint['endpoint'] + request.path[1:],
            headers={key: value for (key, value) in request.headers if key != 'Host'},
            data=request_body,
            cookies=request.cookies,
            timeout=endpoint['timeout'],
            allow_redirects=False)
        content_type = response.headers.get('Content-Type', '')
        encoding = 'utf-8' if is_event_stream_content_type(content_type) else (response.encoding if response.encoding else 'utf-8')
        response_text = response.content.decode(encoding)
        app.logger.debug(
            'Received upstream response status=%s content_type=%s encoding=%s body_preview=%r',
            response.status_code,
            content_type,
            encoding,
            response_text[:300],
        )

        if opencc_enabled:
            json_ensure_ascii = False if encoding.lower() in ('utf-8', 'utf8') else True

            if is_json_content_type(content_type):
                response_json = convert_response_json(json.loads(response_text))
                response_text = json.dumps(response_json, ensure_ascii=json_ensure_ascii, separators=(',', ':'))
                response_content = response_text.encode(encoding)
                app.logger.debug(f'Converted response: {response_text}')
                return response_content, response.status_code, get_response_headers(response)

            if is_event_stream_content_type(content_type):
                response_text = convert_event_stream_response(response_text, json_ensure_ascii)
                response_content = response_text.encode(encoding)
                app.logger.debug(f'Converted event stream response: {response_text}')
                return response_content, response.status_code, get_response_headers(response)

        return response.content, response.status_code, get_response_headers(response)
    except Exception as e:
        app.logger.exception(
            'Forward request failed method=%s path=%s upstream=%s upstream_status=%s upstream_content_type=%s body_preview=%r error=%s',
            request.method,
            request.path,
            endpoint['endpoint'],
            response.status_code if response is not None else None,
            response.headers.get('Content-Type', '') if response is not None else None,
            get_body_preview(request_body),
            e,
        )
        return str(e), 500


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def catch_all(path):
    endpoint = get_next_available_endpoint()
    try:
        if endpoint:
            return forward_request(request, endpoint)
        else:
            return 'No available server', 503
    finally:
        if endpoint:
            endpoint['semaphore'].release()


if __name__ == '__main__':
    try:
        multiprocessing.set_start_method('spawn')
    except RuntimeError:
        pass
    app.run(host=args.listen_host, port=args.listen_port)
