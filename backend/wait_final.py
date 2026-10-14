"""阻塞等待 after_resume 完成，打印最终质量统计。"""
import time
import os

LOG = "/app/after_resume.log"
while True:
    if os.path.exists(LOG):
        data = open(LOG, encoding="utf-8", errors="ignore").read()
        if "FINAL DONE" in data:
            for line in data.splitlines():
                if "QUALITY STATS" in line or "screen done" in line or "zones done" in line:
                    print(line)
            break
    time.sleep(120)
