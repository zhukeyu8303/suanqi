# SuanQi

SuanQi 是一个本地命令驱动的云计算任务执行工具，用于自动创建云服务器、上传 Python 任务、远程运行、回传结果并释放实例。

## 安装

```bash
pip install suanqi
```

## 常用命令

```bash
suanqi useos
suanqi run main.py --return output.txt
suanqi run main.py --cpu 32 --memory 64 --maxusetime 2h
suanqi attach ins-xxxxxxxx
suanqi --list
suanqi release ins-xxxxxxxx
```

## v0.1.4

- 修复 COS 上传后服务端自销毁未触发的问题。
- worker 在退出前同步调用腾讯云 TerminateInstances。
- COS 上传失败不再阻止实例释放。
