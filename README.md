# OCI IP Changer - 甲骨文云一键更换 IP 机器人

一个基于 **Python Flask + pyTelegramBotAPI** 的自动化工具，帮助甲骨文云（Oracle Cloud）用户通过 Telegram 机器人快速完成 IP 更换。项目同时提供管理员网页后台，用于授权用户、分配实例与管理可用次数。

---

## ✨ 功能特性

- 🤖 **Telegram 机器人操作**：用户发送指令即可自助换 IP
- 🛠️ **管理后台**：管理员可授权用户、绑定机器、设置配额
- 🔐 **动态验证码登录**：后台登录通过 Telegram 接收 6 位验证码
- 🔁 **可持久化运行**：支持 `systemd` 守护进程开机自启

---

## 📦 部署流程

> 以下命令以 **Ubuntu / Debian** 为例。

### 1) 登录 VPS 并拉取代码

先确保系统已安装 Git：

```bash
sudo apt update
sudo apt install git -y
```

首次部署：

```bash
git clone https://github.com/GeniusZeroTwo/oci-ip-changer.git
cd oci-ip-changer
```

更新现有版本：

```bash
cd oci-ip-changer
git pull origin main
```

---

### 2) 配置 Python 虚拟环境与依赖

安装基础环境：

```bash
sudo apt install python3 python3-venv python3-pip -y
```

创建并激活虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
```

> 激活后命令行前会出现 `(venv)`。

安装依赖：

```bash
pip install -r requirements.txt
pip install pyyaml
```

---
# 将其添加到 requirements.txt 中，方便以后迁移
```bash
echo "pyyaml" >> /root/oci-ip-changer/requirements.txt

请按注释填写真实参数：

- `TG_BOT_TOKEN`：Telegram 机器人 Token
- `ADMIN_TG_ID`：管理员 Telegram 数字 ID（用于通知与登录）
- OCI 相关配置：
  - `USER_OCID`
  - `TENANCY_OCID`
  - `FINGERPRINT`
  - `REGION`
  - `KEY_FILE`（`private_key.pem` 绝对路径）

`nano` 保存退出：

- `Ctrl + O` 保存
- `Enter` 确认
- `Ctrl + X` 退出

---

### 4) 前台试运行与测试

先在前台启动一次，确认程序无报错：

```bash
python3 app.py
```

若输出包含如下信息，说明运行正常：

```text
Running on http://0.0.0.0:5000
```

停止测试：

```bash
# 在当前终端按
Ctrl + C
```

---

### 5) 配置 systemd 守护进程（推荐）

创建服务文件：

```bash
sudo nano /etc/systemd/system/oci-ip.service
```

写入以下内容（务必按你的实际路径修改 `WorkingDirectory` 与 `ExecStart`）：

```ini
[Unit]
Description=OCI IP Changer Web Server and Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/oci-ip-changer
ExecStart=/root/oci-ip-changer/venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

重新加载并启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl start oci-ip
sudo systemctl enable oci-ip
```

查看服务状态：

```bash
sudo systemctl status oci-ip
```

查看实时日志：

```bash
sudo journalctl -u oci-ip -f
```

---

## 📖 使用指南

### 管理员登录

1. 访问：`http://你的VPS公网IP:5000`
2. 输入管理员 Telegram ID
3. 在机器人中接收 6 位动态验证码并完成登录

### 授权用户

在后台中：

- 填写用户 Telegram ID
- 分配实例/服务器
- 设置可用次数

### 客户操作

引导用户在机器人中发送：

```text
/start
```

按提示即可执行换 IP。

---

## 🛡️ 安全建议

- **严禁泄露敏感文件**：不要上传 `.env`、`.pem` 到 GitHub。
- **限制公网暴露**：建议在 OCI 安全组中限制 `5000` 端口来源。
- **推荐反向代理**：使用 Nginx + HTTPS（如 Let's Encrypt）保护后台。

---

## 🧰 常用命令速查

```bash
# 启动虚拟环境
source venv/bin/activate

# 前台运行（调试）
python3 app.py

# 重启服务
sudo systemctl restart oci-ip

# 查看服务状态
sudo systemctl status oci-ip

# 查看日志
sudo journalctl -u oci-ip -f
```

---

## 📄 License

如需开源发布，建议补充 `LICENSE` 文件并在此处声明许可证类型。
