# 使用指定的基础镜像
FROM lcax200000/python-node-nest:latest

# 设置工作目录
WORKDIR /app

# 复制当前目录的所有内容到容器的 /app 目录
COPY . /app

# 安装 Python 依赖
#RUN pip3 install --no-cache-dir -r requirements.txt -i  https://pypi.tuna.tsinghua.edu.cn/simple

# 暴露端口 8000
EXPOSE 8000

# 设置启动命令
CMD ["python3", "run.py"]

