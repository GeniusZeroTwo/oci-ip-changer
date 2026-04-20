import os
import json
import random
import time
import requests
import threading
import oci
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

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

# 全局变量，用于存储自动获取的服务器列表
servers = {}
active_server_name = None
active_server_ocid = None

otp_store = {}
STATS_FILE = 'stats.json'

bot = telebot.TeleBot(TG_BOT_TOKEN)

# ==========================================
# 核心新增：自动获取 OCI 实例列表
# ==========================================
def fetch_oci_instances():
    global servers, active_server_name, active_server_ocid
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        # 获取当前租户(或指定Compartment)下的所有实例
        instances_data = compute_client.list_instances(compartment_id=OCI_CONFIG["tenancy"]).data
        
        new_servers = {}
        for inst in instances_data:
            # 只提取正在运行的机器 (过滤掉已终止或停止的机器)
            if inst.lifecycle_state == 'RUNNING':
                new_servers[inst.display_name] = inst.id
                
        if new_servers:
            servers = new_servers
            # 如果当前没有选中活跃节点，或者选中的节点已经被删了，就默认选中列表里的第一个
            if active_server_name not in servers:
                active_server_name = list(servers.keys())[0]
                active_server_ocid = servers[active_server_name]
            return True, f"✅ 成功拉取 {len(servers)} 台运行中的服务器！"
        else:
            return False, "⚠️ 未发现任何运行中的服务器。"
    except Exception as e:
        print(f"拉取实例失败: {e}")
        return False, f"❌ 拉取实例失败，请检查 OCI API 权限或网络: {e}"

# 服务启动时，先自动拉取一次
fetch_oci_instances()

# --- Telegram 交互控制端 ---
@bot.message_handler(commands=['sync'])
def command_sync(message):
    """管理员手动触发同步最新服务器列表"""
    if str(message.chat.id) != str(ADMIN_ID): return
    bot.send_message(message.chat.id, "⏳ 正在与甲骨文云通信，拉取最新服务器列表...")
    success, msg = fetch_oci_instances()
    bot.send_message(message.chat.id, msg)

@bot.message_handler(commands=['servers', 'menu', 'start'])
def send_server_menu(message):
    if str(message.chat.id) != str(ADMIN_ID): return
    
    if not servers:
        bot.send_message(message.chat.id, "⚠️ 当前没有可用的服务器。请尝试发送 /sync 重新同步。")
        return

    markup = InlineKeyboardMarkup()
    for name, ocid in servers.items():
        prefix = "✅ " if ocid == active_server_ocid else "⚪ "
        markup.add(InlineKeyboardButton(f"{prefix}{name}", callback_data=f"set_{name}"))
    
    bot.send_message(message.chat.id, "🎛️ **OCI 服务器控制台**\n\n请选择网页端一键换 IP 默认操作的服务器：\n(如新增了机器，请发送 /sync 刷新)", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_'))
def handle_server_selection(call):
    global active_server_ocid, active_server_name
    if str(call.message.chat.id) != str(ADMIN_ID): return
    
    selected_name = call.data[4:]
    if selected_name in servers:
        active_server_name = selected_name
        active_server_ocid = servers[selected_name]
        
        bot.answer_callback_query(call.id, f"已切换至：{selected_name}")
        
        markup = InlineKeyboardMarkup()
        for name, ocid in servers.items():
            prefix = "✅ " if ocid == active_server_ocid else "⚪ "
            markup.add(InlineKeyboardButton(f"{prefix}{name}", callback_data=f"set_{name}"))
        
        bot.edit_message_text(f"🎛️ **OCI 服务器控制台**\n\n当前绑定操作实例：`{selected_name}`", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              reply_markup=markup, parse_mode="Markdown")

def run_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            time.sleep(3)

threading.Thread(target=run_bot_polling, daemon=True).start()

# --- 基础工具函数 ---
def send_tg_message(chat_id, text):
    if not chat_id: return
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception as e:
        print(f"TG消息发送失败: {e}")

def load_stats():
    if not os.path.exists(STATS_FILE): return {"total_changes": 0, "history": []}
    with open(STATS_FILE, 'r') as f: return json.load(f)

def log_change(user_id, server_name, old_ip, new_ip):
    stats = load_stats()
    stats["total_changes"] += 1
    stats["history"].append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id, "server": server_name, "old_ip": old_ip, "new_ip": new_ip
    })
    with open(STATS_FILE, 'w') as f: json.dump(stats, f, indent=4)
    return stats["total_changes"]

# --- OCI 换 IP 核心逻辑 ---
def change_oracle_ip():
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        vnc_client = oci.core.VirtualNetworkClient(OCI_CONFIG)

        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=OCI_CONFIG["tenancy"], instance_id=active_server_ocid).data
        vnic_id = vnic_attachments[0].vnic_id
        private_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        primary_private_ip_id = private_ips[0].id

        public_ips = vnc_client.list_public_ips(
            scope="REGION", compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL").data
        
        old_ip = "Unknown"
        for ip in public_ips:
            if ip.private_ip_id == primary_private_ip_id:
                old_ip = ip.ip_address
                vnc_client.delete_public_ip(ip.id)
                break

        create_details = oci.core.models.CreatePublicIpDetails(
            compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL", private_ip_id=primary_private_ip_id)
        new_ip = vnc_client.create_public_ip(create_details).data.ip_address
        
        return old_ip, new_ip
    except Exception as e:
        print(f"OCI API 错误: {e}")
        return None, None

# --- Web API 路由 ---
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
    otp_store[tg_id] = {"code": code, "expires": time.time() + 300}

    send_tg_message(tg_id, f"🔐 您的网页端身份验证码为：`{code}`")
    return jsonify({"success": True, "message": "验证码已发送到您的 Telegram，请查收！"})

@app.route('/api/change-ip', methods=['POST'])
def handle_change_ip():
    data = request.json
    tg_id = int(data.get('tg_id', 0))
    code = str(data.get('code', ''))

    if tg_id not in ALLOWED_USERS:
        return jsonify({"success": False, "error": "未经授权的用户！"})
    
    record = otp_store.get(tg_id)
    if not record or time.time() > record['expires'] or record['code'] != code:
        return jsonify({"success": False, "error": "验证码错误或已过期！请重新获取。"})

    if not active_server_ocid:
        return jsonify({"success": False, "error": "无法获取服务器列表，请联系管理员！"})

    del otp_store[tg_id]

    old_ip, new_ip = change_oracle_ip()
    
    if new_ip:
        count = log_change(tg_id, active_server_name, old_ip, new_ip)
        
        admin_report = (
            f"📢 **系统通知：IP 已更换**\n\n"
            f"👤 操作客户: `{tg_id}`\n"
            f"🖥️ 目标实例: `{active_server_name}`\n"
            f"🔄 弃用 IP: `{old_ip}`\n"
            f"🌐 启用 IP: `{new_ip}`\n\n"
            f"📊 累计更换总次数: `{count}`"
        )
        send_tg_message(ADMIN_ID, admin_report)
        send_tg_message(tg_id, f"✅ IP 更换成功！\n\n🖥️ 处理节点: `{active_server_name}`\n🌐 新 IP 地址: `{new_ip}`")
        
        return jsonify({"success": True, "new_ip": new_ip})
    else:
        return jsonify({"success": False, "error": "IP 更换失败，请检查服务器网络或 API 限制。"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
