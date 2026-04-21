import os
import json
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
ADMIN_WEB_PWD = os.getenv('ADMIN_WEB_PASSWORD', 'admin123')

OCI_CONFIG = {
    "user": os.getenv('OCI_USER_OCID'),
    "key_file": os.getenv('OCI_KEY_FILE'),
    "fingerprint": os.getenv('OCI_FINGERPRINT'),
    "tenancy": os.getenv('OCI_TENANCY_OCID'),
    "region": os.getenv('OCI_REGION')
}

# 缓存服务器列表 { "display_name": "ocid" }
servers = {}
STATS_FILE = 'stats.json'
PERMS_FILE = 'permissions.json'

bot = telebot.TeleBot(TG_BOT_TOKEN)

# --- 文件持久化管理 ---
def load_permissions():
    """读取用户权限文件格式: {"tg_id": ["ocid1", "ocid2"]}"""
    if not os.path.exists(PERMS_FILE): return {}
    with open(PERMS_FILE, 'r') as f: return json.load(f)

def save_permissions(data):
    with open(PERMS_FILE, 'w') as f: json.dump(data, f, indent=4)

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

def send_tg_message(chat_id, text):
    if not chat_id: return
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception as e:
        pass

# --- OCI API 操作 ---
def fetch_oci_instances():
    global servers
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        instances_data = compute_client.list_instances(compartment_id=OCI_CONFIG["tenancy"]).data
        
        new_servers = {}
        for inst in instances_data:
            if inst.lifecycle_state == 'RUNNING':
                new_servers[inst.display_name] = inst.id
                
        if new_servers: servers = new_servers
        return True, "同步成功"
    except Exception as e:
        return False, str(e)

def change_oracle_ip(target_ocid):
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        vnc_client = oci.core.VirtualNetworkClient(OCI_CONFIG)

        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=OCI_CONFIG["tenancy"], instance_id=target_ocid).data
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

# 启动时抓取一次
fetch_oci_instances()

# ==========================================
# Telegram 机器人交互端 (客户端)
# ==========================================
@bot.message_handler(commands=['start', 'menu'])
def user_menu(message):
    user_id = str(message.chat.id)
    perms = load_permissions()
    allowed_ocids = perms.get(user_id, [])

    if not allowed_ocids:
        bot.send_message(message.chat.id, "❌ **权限拒绝**\n您当前没有任何授权可操作的服务器。请联系管理员分配。")
        return

    markup = InlineKeyboardMarkup()
    for ocid in allowed_ocids:
        # 反查服务器名称
        name = next((n for n, o in servers.items() if o == ocid), "未知节点")
        markup.add(InlineKeyboardButton(f"🔄 更换 {name} IP", callback_data=f"ip_{ocid}"))
    
    bot.send_message(message.chat.id, "🎛️ **您的专属 OCI 控制台**\n\n请选择要更换 IP 的服务器：", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ip_'))
def handle_change_ip(call):
    user_id = str(call.message.chat.id)
    target_ocid = call.data[3:] # 去掉前缀 'ip_'
    
    perms = load_permissions()
    if target_ocid not in perms.get(user_id, []):
        bot.answer_callback_query(call.id, "❌ 授权已过期或被管理员撤销！", show_alert=True)
        return
    
    server_name = next((n for n, o in servers.items() if o == target_ocid), "未知节点")
    
    # 界面转圈等待
    bot.edit_message_text(f"⏳ 正在向甲骨文云发送 `{server_name}` 的更换指令，请耐心等待 (约10秒)...", 
                          chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
    
    old_ip, new_ip = change_oracle_ip(target_ocid)
    
    if new_ip:
        count = log_change(user_id, server_name, old_ip, new_ip)
        
        # 给客户推送结果
        bot.edit_message_text(f"✅ **IP 更换成功！**\n\n🖥️ 节点: `{server_name}`\n🌐 新 IP: `{new_ip}`", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        
        # 给管理员抄送通知
        send_tg_message(ADMIN_ID, f"📢 **系统通知：客户执行换IP**\n\n👤 客户 ID: `{user_id}`\n🖥️ 节点: `{server_name}`\n🔄 旧 IP: `{old_ip}`\n🌐 新 IP: `{new_ip}`\n📊 系统总更换: `{count}`")
    else:
        bot.edit_message_text("❌ 更换失败，请稍后重试或联系管理员。", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id)

def run_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception:
            time.sleep(3)

threading.Thread(target=run_bot_polling, daemon=True).start()

# ==========================================
# 网页管理后台端 (管理员端)
# ==========================================
@app.route('/')
def index():
    return render_template('index.html')

def check_auth(req):
    return req.json.get('password') == ADMIN_WEB_PWD

@app.route('/api/admin/data', methods=['POST'])
def admin_data():
    if not check_auth(request): return jsonify({"success": False, "error": "密码错误或未授权"})
    return jsonify({"success": True, "servers": servers, "permissions": load_permissions()})

@app.route('/api/admin/sync', methods=['POST'])
def admin_sync():
    if not check_auth(request): return jsonify({"success": False, "error": "密码错误或未授权"})
    success, msg = fetch_oci_instances()
    return jsonify({"success": success, "message": msg, "servers": servers})

@app.route('/api/admin/save', methods=['POST'])
def admin_save():
    if not check_auth(request): return jsonify({"success": False, "error": "密码错误或未授权"})
    
    data = request.json
    target_tg_id = str(data.get('tg_id', '')).strip()
    selected_ocids = data.get('ocids', [])
    
    if not target_tg_id: return jsonify({"success": False, "error": "请指定目标用户的 Telegram ID"})

    perms = load_permissions()
    if selected_ocids:
        perms[target_tg_id] = selected_ocids
    else:
        # 如果没有勾选任何机器，则移除该用户的权限
        perms.pop(target_tg_id, None)
        
    save_permissions(perms)
    return jsonify({"success": True, "message": f"用户 {target_tg_id} 的权限已更新！"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
