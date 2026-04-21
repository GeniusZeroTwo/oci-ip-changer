OCI IP Changer - 甲骨文云一键更换 IP 机器人这是一个基于 Python Flask 和 pyTelegramBotAPI 开发的工具，旨在通过 Telegram 机器人为甲骨文云（Oracle Cloud）用户提供简单、快速的 IP 更换服务。系统包含一个管理员网页后台，用于授权用户、分配机器及管理额度。🚀 部署流程第一步：登录 VPS 并拉取代码通过 SSH 连接到你的 VPS，确保系统已安装 git。如果没有安装，请先执行（以 Ubuntu/Debian 为例）：sudo apt update && sudo apt install git -y
获取项目代码：首次部署：git clone [https://github.com/GeniusZeroTwo/oci-ip-changer.git](https://github.com/GeniusZeroTwo/oci-ip-changer.git)
cd oci-ip-changer
更新现有版本：cd oci-ip-changer
git pull origin main
第二步：配置 Python 虚拟环境与依赖为了保持系统环境整洁，建议使用虚拟环境（venv）进行部署。安装必要环境包：sudo apt install python3 python3-venv python3-pip -y
创建并激活虚拟环境：# 在项目根目录下执行
python3 -m venv venv
source venv/bin/activate
激活成功后，命令行提示符前会显示 (venv) 字样。安装依赖：pip install -r requirements.txt
第三步：配置核心安全信息 (.env)这是程序运行的核心，请务必准确填写。复制配置文件模板：cp .env.example .env
编辑配置信息：nano .env
填入真实数据：TG_BOT_TOKEN: 您的 Telegram 机器人 Token。ADMIN_TG_ID: 您的 Telegram 数字 ID（用于接收安全通知和验证码）。OCI 配置: 填入甲骨文云的 USER_OCID、TENANCY_OCID、FINGERPRINT、REGION 以及 private_key.pem 的绝对路径。按 Ctrl + O 保存，Enter 确认，Ctrl + X 退出。第四步：试运行与测试在正式部署为后台服务前，建议先进行前台测试。# 确保在 (venv) 激活状态下
python3 app.py
观察输出：如果没有红色报错，并显示 Running on http://0.0.0.0:5000，说明程序正常。检查同步：日志中若出现 ✅ 同步成功，说明甲骨文 API 凭证有效。停止测试：按 Ctrl + C 退出。第五步：配置守护进程（持久化运行）使用 Systemd 让程序在后台长期运行，并在 VPS 重启后自动启动。创建服务文件：sudo nano /etc/systemd/system/oci-ip.service
粘贴以下配置（请根据实际路径修改 WorkingDirectory 和 ExecStart）：[Unit]
Description=OCI IP Changer Web Server and Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/oci-ip-changer
# 指向虚拟环境中的 gunicorn 路径
ExecStart=/root/oci-ip-changer/venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
启动并设置自启：sudo systemctl daemon-reload
sudo systemctl start oci-ip
sudo systemctl enable oci-ip
🎉 部署完成！现在，您的服务已经成功上线：管理后台：访问 http://你的VPS公网IP:5000。身份验证：输入管理员 ID，在 Telegram 接收验证码登录。授权操作：在后台为用户分配机器 OCID 和 IP 更换额度。开始使用：引导用户在 Telegram 对机器人发送 /start 即可看到一键换 IP 按钮。🛡️ 安全提示请勿将 .env 或任何 .pem 密钥文件上传至公共仓库。建议在 VPS 防火墙（或甲骨文安全列表）中仅允许特定 IP 访问 5000 端口，或通过 Nginx 配置反向代理增加 SSL。
