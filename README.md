
这是一个基于 Python Flask 和 `pyTelegramBotAPI` 开发的工具，旨在通过 Telegram 机器人为甲骨文云（Oracle Cloud）用户提供简单、快速的 IP 更换服务。系统包含一个管理员网页后台，用于授权用户、分配机器及管理额度。

---

## 🚀 部署流程

### 第一步：登录 VPS 并拉取代码

通过 SSH 连接到你的 VPS。确保你的系统安装了 `git`。如果没有，先执行安装（以 Ubuntu/Debian 为例）：

```bash
sudo apt update && sudo apt install git -y
