import time


for index in range(30):
    print(
        f"当前进度：{index + 1}/30",
        flush=True,
    )
    time.sleep(5)

with open(
    "output.txt",
    "w",
    encoding="utf-8",
) as output_file:
    output_file.write("任务执行完成")

print("结果文件已经生成", flush=True)