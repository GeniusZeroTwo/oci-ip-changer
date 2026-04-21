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
    except Exception:
        pass

# --- OCI API 操作 ---
def fetch_oci_instances():
    global servers
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        instances_data = compute_client.list_instances(compartment_id=OCI_CONFIG["tenancy"]).data
        new_servers = {inst.display_name: inst.id for inst in instances_data if inst.lifecycle_state == 'RUNNING'}
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
        if not vnic_attachments: raise Exception("未找到该实例的网卡 (VNIC)")
        vnic_id = vnic_attachments[0].vnic_id
        
        private_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        if not private_ips: raise Exception("未找到该实例的内网 IP")
        primary_private_ip_id = private_ips[0].id

        old_ip = "Unknown"
        try:
            get_ip_details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=primary_private_ip_id)
            public_ip = vnc_client.get_public_ip_by_private_ip_id(get_ip_details).data
            old_ip = public_ip.ip_address
            
            if public_ip.lifetime == 'RESERVED':
                vnc_client.update_public_ip(public_ip.id, oci.core.models.UpdatePublicIpDetails(private_ip_id=""))
            else:
                vnc_client.delete_public_ip(public_ip.id)
            time.sleep(2)
        except oci.exceptions.ServiceError as e:
            if e.status == 404: old_ip = "None"
            else: raise e

        create_details = oci.core.models.CreatePublicIpDetails(
            compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL", private_ip_id=primary_private_ip_id)
        new_ip = vnc_client.create_public_ip(create_details).data.ip_address
        return old_ip, new_ip
    except Exception as e:
        error_msg = str(e)
        print(f"OCI API 错误: {error_msg}")
        # 使用三引号，防止任何可能的截断报错
        report = f"""⚠️ **甲骨文 API 报错详情**

```text
{error_msg}
```
目标机器: `{target_ocid}`"""
        send_tg_message(ADMIN_ID, report)
        return None, None

fetch_oci_instances()

# --- Telegram Bot 逻辑 ---
def is_whitelisted(user_id):
    uid = str(user_id)
    return uid == str(ADMIN_ID) or uid in load_permissions()

@bot.message_handler(commands=['start', 'menu'])
def user_menu(message):
    if not is_whitelisted(message.chat.id): return
    perms = load_permissions().get(str(message.chat.id), {})
    ocids = perms.get('ocids', [])
    rem = perms.get('max_changes', 0) - perms.get('used_changes', 0)

    if not ocids:
        bot.send_message(message.chat.id, "❌ **权限拒绝**\n您当前没有任何授权可操作的服务器。")
        return
    if rem <= 0:
        bot.send_message(message.chat.id, "⚠️ **额度耗尽**\n请联系管理员充值。")
        return

    markup = InlineKeyboardMarkup()
    for ocid in ocids:
        name = next((n for n, o in servers.items() if o == ocid), "未知节点")
        markup.add(InlineKeyboardButton(f"🔄 更换 {name} IP", callback_data=f"ip_{get_short_id(ocid)}"))
    
    bot.send_message(message.chat.id, f"🎛️ **OCI 控制台**\n📊 剩余额度：`{rem}` 次\n请选择：", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ip_'))
def handle_change_ip(call):
    if not is_whitelisted(call.message.chat.id): return
    uid = str(call.message.chat.id)
    sid = call.data[3:]
    target_ocid = next((o for n, o in servers.items() if get_short_id(o) == sid), None)
    
    perms = load_permissions()
    user_data = perms.get(uid, {})
    if not target_ocid or target_ocid not in user_data.get('ocids', []):
        bot.answer_callback_query(call.id, "❌ 无权操作或实例不存在", show_alert=True)
        return
    
    if user_data.get('used_changes', 0) >= user_data.get('max_changes', 0):
        bot.answer_callback_query(call.id, "❌ 额度已用完", show_alert=True)
        return

    s_name = next((n for n, o in servers.items() if o == target_ocid), "未知")
    bot.edit_message_text(f"⏳ 正在更换 `{s_name}` IP...", chat_id=call.message.chat.id, message_id=call.message.message_id)
    
    old, new = change_oracle_ip(target_ocid)
    if new:
        perms[uid]['used_changes'] += 1
        save_permissions(perms)
        count = log_change(uid, s_name, old, new)
        bot.edit_message_text(f"✅ **成功！**\n🌐 新 IP: `{new}`\n📊 剩余: `{perms[uid]['max_changes'] - perms[uid]['used_changes']}` 次", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        send_tg_message(ADMIN_ID, f"🟢 **换IP成功**\n用户: `{uid}`\n节点: `{s_name}`\n新IP: `{new}`")
    else:
        bot.edit_message_text("❌ 更换失败，本次不扣额度。", chat_id=call.message.chat.id, message_id=call.message.message_id)
        send_tg_message(ADMIN_ID, f"🔴 **换IP失败**\n用户: `{uid}`\n节点: `{s_name}`")

threading.Thread(target=lambda: bot.infinity_polling(timeout=10), daemon=True).start()

# --- Flask 管理后台 ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/admin/send-code', methods=['POST'])
def admin_send_code():
    tg_id = str(request.json.get('tg_id', ''))
    if tg_id != str(ADMIN_ID): return jsonify({"success": False, "error": "ID不匹配"})
    code = str(random.randint(100000, 999999))
    admin_session.update({"code": code, "expires": time.time() + 7200})
    send_tg_message(ADMIN_ID, f"🔐 验证码：`{code}`")
    return jsonify({"success": True})

@app.route('/api/admin/data', methods=['POST'])
def admin_data():
    code = request.json.get('code')
    if not code or code != admin_session.get('code'): return jsonify({"success": False, "error": "验证失败"})
    return jsonify({"success": True, "servers": servers, "permissions": load_permissions()})

@app.route('/api/admin/sync', methods=['POST'])
def admin_sync():
    fetch_oci_instances()
    return jsonify({"success": True, "servers": servers})

@app.route('/api/admin/save', methods=['POST'])
def admin_save():
    d = request.json
    uid = str(d.get('tg_id', '')).strip()
    perms = load_permissions()
    perms[uid] = {"ocids": d.get('ocids', []), "max_changes": int(d.get('max_changes', 0)), "used_changes": perms.get(uid, {}).get('used_changes', 0)}
    save_permissions(perms)
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
