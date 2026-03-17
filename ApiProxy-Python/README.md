## 安装依赖
```
pip install -r requirements.txt
```

## 配置文件

在项目根目录下，有一个`config.json`文件，用于配置服务器和OpenCC设置。示例如下：
```json
{
  "enable_opencc": false,
  "opencc_config": "s2t.json",
  "endpoints": [
    {
      "endpoint": "http://127.0.0.1:8080/",
      "max_concurrency": 16,
      "timeout": 60
    }
  ]
}
```
- `enable_opencc`: 是否启用OpenCC转换。
- `opencc_config`: OpenCC配置文件（预设配置文件名参考OpenCC项目的[预设配置文件](https://github.com/BYVoid/OpenCC?tab=readme-ov-file#%E9%A0%90%E8%A8%AD%E9%85%8D%E7%BD%AE%E6%96%87%E4%BB%B6)部分）。
- `endpoints`: 服务器列表，每个端点包含以下字段：
  - `endpoint`: 服务器地址。
  - `max_concurrency`: 最大并发数。
  - `timeout`: 请求超时时间（秒）。

## 运行

在项目根目录下运行以下命令启动服务器：
```
python app.py
```
Windows用户也可以使用Releases中pyinstaller打包的可执行文件：
```
app.exe
```
可选参数：
- `-c`或`--config`: 配置文件路径（默认`config.json`）。
- `-l`或`--listen_host`: 监听主机地址（默认`127.0.0.1`）。
- `-p`或`--listen_port`: 监听端口（默认`8081`）。
- `-d`或`--debug`: 启用调试模式。
- `--trust-env`或`--no-trust-env`: 是否信任环境变量和系统代理设置来访问上游（默认`--no-trust-env`）。

## 说明
当前支持completion api和chat completion api
