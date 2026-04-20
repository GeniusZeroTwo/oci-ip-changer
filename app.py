import os
import json
import random
import time
import requests
import oci
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- 配置读取 ---
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_TG_ID')
ALLOWED_USERS = [int(uid.strip()) for uid in os.getenv('ALLOWED_TG_USERS', '').split(',') if uid.strip()]

OCI_CONFIG = {
    "user": os.getenv('OCI_USER_OCID'),
    "key_file": os.getenv('OCI_KEY_FILE'),
    "fingerprint": os.getenv('OCI_FINGERPRINT'),
    "tenancy": os.getenv('OCI_TENANCY_OCID'),
    "region": os.getenv('OCI_REGION')
}
INSTANCE_OCID = os.getenv('OCI_INSTANCE_OCID')

# 内存中的验证码存储
otp_store = {}
STATS_FILE = 'stats.json'

# --- 基础工具函数 ---
def send_tg_message(chat_id, text):
    if not chat_id: return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"TG消息发送失败: {e}")

def load_stats():
    if not os.path.exists(STATS_FILE):
        return {"total_changes": 0, "history": []}
    with open(STATS_FILE, 'r') as f:
        return json.load(f)

def save_stats(stats):
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=4)

def log_change(user_id, old_ip, new_ip):
    stats = load_stats()
    stats["total_changes"] += 1
    stats["history"].append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "old_ip": old_ip,
        "new_ip": new_ip
    })
    save_stats(stats)
    return stats["total_changes"]

# --- OCI 换 IP 逻辑 ---
def change_oracle_ip():
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        vnc_client = oci.core.VirtualNetworkClient(OCI_CONFIG)

        # 获取实例绑定的 VNIC
        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=OCI_CONFIG["tenancy"], instance_id=INSTANCE_OCID).data
        vnic_id = vnic_attachments[0].vnic_id
        private_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        primary_private_ip_id = private_ips[0].id

        # 查找当前公网 IP 并解绑删除
        public_ips = vnc_client.list_public_ips(
            scope="REGION", compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL").data
        
        old_ip = "Unknown"
        for ip in public_ips:
            if ip.private_ip_id == primary_private_ip_id:
                old_ip = ip.ip_address
                vnc_client.delete_public_ip(ip.id)
                break

        # 申请并绑定新 IP
        create_details = oci.core.models.CreatePublicIpDetails(
            compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL", private_ip_id=primary_private_ip_id)
        new_ip = vnc_client.create_public_ip(create_details).data.ip_address
        
        return old_ip, new_ip
    except Exception as e:
        print(f"OCI API 错误: {e}")
        return None, None

# --- Web 路由配置 ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.json
    tg_id = int(data.get('tg_id', 0))

    if tg_id not in ALLOWED_USERS:
        return jsonify({"success": False, "error": "未经授权的用户！"})

    code = str(random.randint(100000, 999999))
    otp_store[tg_id] = {"code": code, "expires": time.time() + 300} # 5分钟有效期

    # 仅向当前操作客户发送验证码
    send_tg_message(tg_id, f"🔐 您的网页端身份验证码为：`{code}`\n\n该验证码 5 分钟内有效。如非本人操作请忽略。")
    return jsonify({"success": True, "message": "验证码已发送到您的 Telegram，请查收！"})

@app.route('/api/change-ip', methods=['POST'])
def handle_change_ip():
    data = request.json
    tg_id = int(data.get('tg_id', 0))
    code = str(data.get('code', ''))

    # 1. 鉴权
    if tg_id not in ALLOWED_USERS:
        return jsonify({"success": False, "error": "未经授权的用户！"})
    
    record = otp_store.get(tg_id)
    if not record or time.time() > record['expires'] or record['code'] != code:
        return jsonify({"success": False, "error": "验证码错误或已过期！请重新获取。"})

    del otp_store[tg_id] # 消耗验证码

    # 2. 执行换 IP
    old_ip, new_ip = change_oracle_ip()
    
    if new_ip:
        # 3. 记录日志获取累计次数
        count = log_change(tg_id, old_ip, new_ip)
        
        # 4. 向管理员推送完整报告
        admin_report = (
            f"📢 **系统通知：IP 已更换**\n\n"
            f"👤 操作客户 ID: `{tg_id}`\n"
            f"🔄 弃用 IP: `{old_ip}`\n"
            f"🌐 启用 IP: `{new_ip}`\n\n"
            f"📊 累计更换总次数: `{count}`"
        )
        send_tg_message(ADMIN_ID, admin_report)

        # 5. 向客户推送结果
        send_tg_message(tg_id, f"✅ IP 更换成功！\n\n🌐 您的新 IP 地址为: `{new_ip}`")
        
        return jsonify({"success": True, "new_ip": new_ip})
    else:
        return jsonify({"success": False, "error": "IP 更换失败，请检查服务器网络或 API 限制。"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
