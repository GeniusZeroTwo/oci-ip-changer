import os
import json
import time
import random
import threading
import hashlib
import oci
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

# 加载配置
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- 配置项 ---
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_TG_ID')

OCI_CONFIG = {
    "user": os.getenv('OCI_USER_OCID'),
    "key_file": os.getenv('OCI_KEY_FILE'),
    "fingerprint": os.getenv('OCI_FINGERPRINT'),
    "tenancy": os.getenv('OCI_TENANCY_OCID'),
    "region": os.getenv('OCI_REGION')
}

# 内存变量
servers = {}
admin_session = {"code": None, "expires": 0}
STATS_FILE = 'stats.json'
PERMS_FILE = 'permissions.json'

bot = telebot.TeleBot(TG_BOT_TOKEN)

# --- 工具函数 ---
def get_short_id(text):
    return hashlib.md5(str(text).encode()).hexdigest()[:16]

def load_permissions():
    if not os.path.exists(PERMS_FILE): return {}
    try:
        with open(PERMS_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_permissions(data):
    with open(PERMS_FILE, 'w') as f: json.dump(data, f, indent=4)

def load_stats():
    if not os.path.exists(STATS_FILE): return {"total_changes": 0, "history": []}
    try:
        with open(STATS_FILE, 'r') as f: return json.load(f)
    except: return {"total_changes": 0, "history": []}

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
    except: pass

# --- OCI 核心逻辑 ---
def fetch_oci_instances():
    global servers
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        data = compute_client.list_instances(compartment_id=OCI_CONFIG["tenancy"]).data
        servers = {i.display_name: i.id for i in data if i.lifecycle_state == 'RUNNING'}
        return True, "同步成功"
    except Exception as e:
        return False, str(e)

def change_oracle_ip(target_ocid):
    try:
        compute_client = oci.core.ComputeClient(OCI_CONFIG)
        vnc_client = oci.core.VirtualNetworkClient(OCI_CONFIG)

        # 获取 VNIC
        vnic_attach = compute_client.list_vnic_attachments(
            compartment_id=OCI_CONFIG["tenancy"], instance_id=target_ocid).data
        if not vnic_attach: raise Exception("未找到网卡")
        vnic_id = vnic_attach[0].vnic_id
        
        # 获取 Private IP
        p_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        if not p_ips: raise Exception("未找到内网IP")
        p_ip_id = p_ips[0].id

        old_ip = "Unknown"
        # 智能清理现有公网 IP
        try:
            get_details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=p_ip_id)
            pub_ip = vnc_client.get_public_ip_by_private_ip_id(get_details).data
            old_ip = pub_ip.ip_address
            
            if pub_ip.lifetime == 'RESERVED':
                # 保留 IP 执行解绑操作
                vnc_client.update_public_ip(pub_ip.id, oci.core.models.UpdatePublicIpDetails(private_ip_id=""))
            else:
                # 临时 IP 执行删除操作
                vnc_client.delete_public_ip(pub_ip.id)
            time.sleep(2) # 强制缓冲防止冲突
        except oci.exceptions.ServiceError as e:
            if e.status == 404: old_ip = "None"
            else: raise e

        # 申请新临时 IP
        create_info = oci.core.models.CreatePublicIpDetails(
            compartment_id=OCI_CONFIG["tenancy"], lifetime="EPHEMERAL", private_ip_id=p_ip_id)
        new_ip = vnc_client.create_public_ip(create_info).data.ip_address
        return old_ip, new_ip
    except Exception as e:
        error_msg = str(e)
        # 使用三引号块，防止传输截断导致语法错误
        report = f"""⚠️ **OCI 报错详情**
```text
{error_msg}
```
实例ID: `{target_ocid}`"""
        send_tg_message(ADMIN_ID, report)
        return None, None

# 启动时同步一次服务器列表
fetch_oci_instances()

# --- Telegram 机器人端 ---
def is_auth(uid):
    uid = str(uid)
    return uid == str(ADMIN_ID) or uid in load_permissions()

@bot.message_handler(commands=['start', 'menu'])
def user_menu(message):
    if not is_auth(message.chat.id): return
    uid = str(message.chat.id)
    perms = load_permissions().get(uid, {})
    ocids = perms.get('ocids', [])
    rem = perms.get('max_changes', 0) - perms.get('used_changes', 0)

    if not ocids:
        bot.send_message(uid, "❌ 您暂无可用服务器授权")
        return
    if rem <= 0:
        bot.send_message(uid, "⚠️ 您的额度已耗尽")
        return

    markup = InlineKeyboardMarkup()
    for o in ocids:
        name = next((n for n, id in servers.items() if id == o), "未知节点")
        markup.add(InlineKeyboardButton(f"🔄 更换 {name} IP", callback_data=f"ip_{get_short_id(o)}"))
    
    bot.send_message(uid, f"🎛️ **OCI 控制台**\n📊 剩余额度：`{rem}` 次", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ip_'))
def handle_ip_btn(call):
    uid = str(call.message.chat.id)
    if not is_auth(uid): return
    
    sid = call.data[3:]
    target_ocid = next((o for n, o in servers.items() if get_short_id(o) == sid), None)
    
    perms = load_permissions()
    u_data = perms.get(uid, {})
    if not target_ocid or target_ocid not in u_data.get('ocids', []):
        bot.answer_callback_query(call.id, "无权操作或实例无效", show_alert=True)
        return
    
    if u_data.get('used_changes', 0) >= u_data.get('max_changes', 0):
        bot.answer_callback_query(call.id, "额度不足", show_alert=True)
        return

    s_name = next((n for n, o in servers.items() if o == target_ocid), "未知")
    bot.edit_message_text(f"⏳ 正在请求 API 更换 `{s_name}` IP...", chat_id=uid, message_id=call.message.message_id)
    
    old, new = change_oracle_ip(target_ocid)
    if new:
        perms[uid]['used_changes'] += 1
        save_permissions(perms)
        log_change(uid, s_name, old, new)
        bot.edit_message_text(f"✅ **更换成功！**\n🌐 新IP: `{new}`\n📊 剩余: `{perms[uid]['max_changes'] - perms[uid]['used_changes']}` 次", chat_id=uid, message_id=call.message.message_id, parse_mode="Markdown")
        send_tg_message(ADMIN_ID, f"🟢 **换IP通知**\n用户: `{uid}`\n节点: `{s_name}`\n新IP: `{new}`")
    else:
        bot.edit_message_text("❌ 更换失败，请检查管理员通知或稍后再试。", chat_id=uid, message_id=call.message.message_id)
        send_tg_message(ADMIN_ID, f"🔴 **换IP失败**\n用户: `{uid}`\n节点: `{s_name}`")

# 启动轮询
threading.Thread(target=lambda: bot.infinity_polling(timeout=20), daemon=True).start()

# --- Flask Web 后台路由 ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/admin/send-code', methods=['POST'])
def send_code():
    # 改动：不再需要前端传入管理员 ID，直接从 .env 配置读取唯一管理员 ID 发送
    code = str(random.randint(100000, 999999))
    admin_session.update({"code": code, "expires": time.time() + 3600})
    send_tg_message(ADMIN_ID, f"🔐 后台验证码：`{code}`")
    return jsonify({"success": True})

@app.route('/api/admin/data', methods=['POST'])
def get_data():
    if request.json.get('code') != admin_session.get('code'): return jsonify({"success": False})
    return jsonify({"success": True, "servers": servers, "permissions": load_permissions()})

@app.route('/api/admin/sync', methods=['POST'])
def sync_data():
    fetch_oci_instances()
    return jsonify({"success": True, "servers": servers})

@app.route('/api/admin/save', methods=['POST'])
def save_data():
    d = request.json
    uid = str(d.get('tg_id', '')).strip()
    if not uid: return jsonify({"success": False})
    p = load_permissions()
    p[uid] = {
        "ocids": d.get('ocids', []),
        "max_changes": int(d.get('max_changes', 0)),
        "used_changes": p.get(uid, {}).get('used_changes', 0)
    }
    save_permissions(p)
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
