# SuanQi 腾讯云网关

## 项目结构

```text
suanqi_gateway_complete/
├── gateway/
│   ├── __init__.py
│   └── tencent_gateway.py
├── main.py
└── requirements.txt
```

## 安装

```bash
pip install -r requirements.txt
```

## PowerShell 配置密钥

```powershell
$env:TENCENTCLOUD_SECRET_ID="你的SecretId"
$env:TENCENTCLOUD_SECRET_KEY="你的SecretKey"
```

## 运行

```bash
python main.py
```

`main.py` 默认只查询和询价，不创建收费实例。

正式测试创建时，将：

```python
CREATE_INSTANCE = False
```

改为：

```python
CREATE_INSTANCE = True
```
