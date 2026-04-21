import os
import json
import time
import random
import requests
import threading
import hashlib
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

OCI_CONFIG = {
    "user": os.getenv('OCI_USER_OCID'),
    "key_file": os.getenv('OCI_KEY_FILE'),
    "fingerprint": os.getenv('OCI_FINGERPRINT'),
    "tenancy": os.getenv('OCI_TENANCY_OCID'),
    "region": os.getenv('OCI_REGION')
}

servers = {}
STATS_FILE = 'stats.json'
PERMS_FILE = 'permissions.json'

# 管理员会话存储 (内存)
admin_session = {"code": None, "expires": 0}

bot = telebot.TeleBot(TG_BOT_TOKEN)

# --- 基础工具函数 ---
def get_short_id(text):
    """将超长的 OCID 压缩为 16 位的短哈希，以符合 Telegram 64 字节限制"""
    return hashlib.md5(str(text).encode()).hexdigest()[:16]

def load_permissions():
    if not os.path.exists(PERMS_FILE): return {}
    with open(PERMS_FILE, 'r') as f: 
        data = json.load(f)
        for k, v in data.items():
            if isinstance(v, list):
                data[k] = {"ocids": v, "max_changes": 0, "used_changes": 0}
        return data

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

fetch_oci_instances()

# ==========================================
# Telegram 机器人交互端 (客户端)
# ==========================================

def is_whitelisted(user_id):
    user_id = str(user_id)
    if user_id == str(ADMIN_ID): return True 
    perms = load_permissions()
    if user_id in perms: return True
    return False

@bot.message_handler(commands=['start', 'menu'])
def user_menu(message):
    if not is_whitelisted(message.chat.id): return

    user_id = str(message.chat.id)
    perms = load_permissions()
    user_data = perms.get(user_id, {})
    allowed_ocids = user_data.get('ocids', [])
    max_changes = user_data.get('max_changes', 0)
    used_changes = user_data.get('used_changes', 0)

    if not allowed_ocids:
        bot.send_message(message.chat.id, "❌ **权限拒绝**\n您当前没有任何授权可操作的服务器。")
        return
        
    remaining = max_changes - used_changes
    if remaining <= 0:
        bot.send_message(message.chat.id, "⚠️ **额度耗尽**\n您的更换 IP 次数已用完，请联系管理员充值。")
        return

    markup = InlineKeyboardMarkup()
    for ocid in allowed_ocids:
        name = next((n for n, o in servers.items() if o == ocid), "未知节点")
        # 修复点：使用短哈希替代长 OCID
        markup.add(InlineKeyboardButton(f"🔄 更换 {name} IP", callback_data=f"ip_{get_short_id(ocid)}"))
    
    bot.send_message(message.chat.id, f"🎛️ **您的专属 OCI 控制台**\n\n📊 当前剩余额度：`{remaining}` 次\n请选择要操作的服务器：", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ip_'))
def handle_change_ip(call):
    if not is_whitelisted(call.message.chat.id):
        bot.answer_callback_query(call.id, "⛔ 非法请求，账号未受信任！", show_alert=True)
        return

    user_id = str(call.message.chat.id)
    short_id = call.data[3:] # 获取短哈希
    
    # 修复点：反向查找真实的 OCID
    target_ocid = None
    for name, ocid in servers.items():
        if get_short_id(ocid) == short_id:
            target_ocid = ocid
            break
            
    if not target_ocid:
        bot.answer_callback_query(call.id, "❌ 找不到对应的服务器实例，可能已被删除！", show_alert=True)
        return

    perms = load_permissions()
    user_data = perms.get(user_id, {})
    
    if target_ocid not in user_data.get('ocids', []):
        bot.answer_callback_query(call.id, "❌ 授权已过期或被撤销！", show_alert=True)
        return
        
    max_changes = user_data.get('max_changes', 0)
    used_changes = user_data.get('used_changes', 0)
    if used_changes >= max_changes:
        bot.answer_callback_query(call.id, "❌ 额度已用完！", show_alert=True)
        bot.edit_message_text("⚠️ **额度耗尽**\n您的更换 IP 额度已用完，请联系管理员充值。", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        return
    
    server_name = next((n for n, o in servers.items() if o == target_ocid), "未知节点")
    
    bot.edit_message_text(f"⏳ 正在向甲骨文云发送 `{server_name}` 的更换指令，请耐心等待...", 
                          chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
    
    old_ip, new_ip = change_oracle_ip(target_ocid)
    
    if new_ip:
        perms[user_id]['used_changes'] += 1
        save_permissions(perms)
        remaining = max_changes - perms[user_id]['used_changes']
        
        count = log_change(user_id, server_name, old_ip, new_ip)
        
        bot.edit_message_text(f"✅ **IP 更换成功！**\n\n🖥️ 节点: `{server_name}`\n🌐 新 IP: `{new_ip}`\n📊 剩余额度: `{remaining}` 次", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        
        send_tg_message(ADMIN_ID, f"📢 **系统通知：客户执行换IP**\n\n👤 客户 ID: `{user_id}`\n🖥️ 节点: `{server_name}`\n🔄 旧 IP: `{old_ip}`\n🌐 新 IP: `{new_ip}`\n💳 该客户剩余额度: `{remaining}`\n📊 系统总更换: `{count}`")
    else:
        bot.edit_message_text("❌ 更换失败 (API抽风或频率限制)。\n**本次操作不扣除您的额度**，请稍后再试。", 
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
    code = str(req.json.get('code', ''))
    if not code or not admin_session.get('code'): return False
    if code == admin_session['code'] and time.time() < admin_session['expires']:
        return True
    return False

@app.route('/api/admin/send-code', methods=['POST'])
def admin_send_code():
    data = request.json
    tg_id = str(data.get('tg_id', '')).strip()
    
    if tg_id != str(ADMIN_ID):
        return jsonify({"success": False, "error": "管理员 ID 不匹配或无权操作！"})
        
    code = str(random.randint(100000, 999999))
    admin_session["code"] = code
    admin_session["expires"] = time.time() + 7200 

    send_tg_message(ADMIN_ID, f"🔐 **后台登录验证码**\n\n您的动态密码为：`{code}`\n\n该验证码在 2 小时内有效。如非本人操作请忽略。")
    return jsonify({"success": True, "message": "验证码已发送至您的 Telegram，请查收！"})

@app.route('/api/admin/data', methods=['POST'])
def admin_data():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误或已过期，请重新获取"})
    return jsonify({"success": True, "servers": servers, "permissions": load_permissions()})

@app.route('/api/admin/sync', methods=['POST'])
def admin_sync():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误或已过期"})
    success, msg = fetch_oci_instances()
    return jsonify({"success": success, "message": msg, "servers": servers})

@app.route('/api/admin/save', methods=['POST'])
def admin_save():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误或已过期"})
    
    data = request.json
    target_tg_id = str(data.get('tg_id', '')).strip()
    selected_ocids = data.get('ocids', [])
    max_changes = int(data.get('max_changes', 0))
    
    if not target_tg_id: return jsonify({"success": False, "error": "请指定目标用户的 Telegram ID"})

    perms = load_permissions()
    
    if selected_ocids or max_changes > 0:
        used_changes = perms.get(target_tg_id, {}).get('used_changes', 0)
        perms[target_tg_id] = {
            "ocids": selected_ocids,
            "max_changes": max_changes,
            "used_changes": used_changes
        }
    else:
        perms.pop(target_tg_id, None)
        
    save_permissions(perms)
    return jsonify({"success": True, "message": f"用户 {target_tg_id} 的权限和额度已更新！"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
