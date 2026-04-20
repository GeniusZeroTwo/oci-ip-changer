import os
import random
import time
import requests
import oci
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

# 加载 .env 环境变量
load_dotenv()

app = Flask(__name__)
CORS(app)

# 读取配置
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
# 解析允许的 TG ID 列表
ALLOWED_USERS = [int(uid.strip()) for uid in os.getenv('ALLOWED_TG_USERS', '').split(',') if uid.strip()]

# OCI 配置字典
OCI_CONFIG = {
    "user": os.getenv('OCI_USER_OCID'),
    "key_file": os.getenv('OCI_KEY_FILE'),
    "fingerprint": os.getenv('OCI_FINGERPRINT'),
    "tenancy": os.getenv('OCI_TENANCY_OCID'),
    "region": os.getenv('OCI_REGION')
}
INSTANCE_OCID = os.getenv('OCI_INSTANCE_OCID')

# 内存中的 OTP 存储: { tg_id: {"code": "123456", "expires": timestamp} }
otp_store = {}

def send_tg_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"TG消息发送失败: {e}")

# --- OCI 换 IP 核心逻辑 (与之前相同，略作精简) ---
def change_oracle_ip():
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        vnc_client = oci.core.VirtualNetworkClient(OCI_CONFIG)

        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=OCI_CONFIG["tenancy"], instance_id=INSTANCE_OCID).data
        vnic_id = vnic_attachments[0].vnic_id
        private_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        primary_private_ip_id = private_ips[0].id

        public_ips = vnc_client.list_public_ips(
            scope="REGION", compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL").data
        
        for ip in public_ips:
            if ip.private_ip_id == primary_private_ip_id:
                vnc_client.delete_public_ip(ip.id)
                break

        create_details = oci.core.models.CreatePublicIpDetails(
            compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL", private_ip_id=primary_private_ip_id)
        new_ip = vnc_client.create_public_ip(create_details).data.ip_address
        return new_ip
    except Exception as e:
        print(f"OCI API 错误: {e}")
        return None

# --- 路由 ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.json
    tg_id = int(data.get('tg_id', 0))

    if tg_id not in ALLOWED_USERS:
        return jsonify({"success": False, "error": "未经授权的用户！您不在白名单中。"})

    # 生成 6 位随机验证码
    code = str(random.randint(100000, 999999))
    # 有效期 5 分钟
    otp_store[tg_id] = {"code": code, "expires": time.time() + 300}

    send_tg_message(tg_id, f"🔐 您的网页端身份验证码为：`{code}`\n\n该验证码在 5 分钟内有效。如非本人操作请忽略。")
    return jsonify({"success": True, "message": "验证码已发送到您的 Telegram，请查收！"})

@app.route('/api/change-ip', methods=['POST'])
def handle_change_ip():
    data = request.json
    tg_id = int(data.get('tg_id', 0))
    code = str(data.get('code', ''))

    # 1. 验证用户
    if tg_id not in ALLOWED_USERS:
        return jsonify({"success": False, "error": "未经授权的用户！"})
    
    # 2. 验证 OTP
    record = otp_store.get(tg_id)
    if not record or time.time() > record['expires'] or record['code'] != code:
        return jsonify({"success": False, "error": "验证码错误或已过期！请重新获取。"})

    # 验证成功，删除验证码，防止重复使用
    del otp_store[tg_id]

    # 3. 执行换 IP
    new_ip = change_oracle_ip()
    if new_ip:
        send_tg_message(tg_id, f"✅ IP 更换成功！\n\n🌐 新 IP 地址: `{new_ip}`")
        return jsonify({"success": True, "new_ip": new_ip})
    else:
        return jsonify({"success": False, "error": "IP 更换失败，请检查服务器日志。"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
