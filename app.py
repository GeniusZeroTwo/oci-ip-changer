import os
import json
import time
import random
import threading
import hashlib
import secrets
import yaml
import oci
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

# 加载配置
load_dotenv()

app = Flask(__name__)
CORS(app)

TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_TG_ID')

ACCOUNTS_FILE = 'oci_accounts.yaml'
STATS_FILE = 'stats.json'
PERMS_FILE = 'permissions.json'
IP_CACHE_FILE = 'ip_cache.json'  # 新增：IP 缓存文件，减少 API 频繁请求

BJ_TZ = timezone(timedelta(hours=8))
servers = {}
instance_config_map = {} 
admin_session = {"code": None, "expires": 0, "attempts": 0}

bot = telebot.TeleBot(TG_BOT_TOKEN)

# ==========================================
# 工具函数 & 存储模块
# ==========================================
def get_bj_now():
    return datetime.now(BJ_TZ)

def get_short_id(text):
    return hashlib.md5(str(text).encode()).hexdigest()[:16]

# --- IP 缓存模块 ---
def load_ip_cache():
    if not os.path.exists(IP_CACHE_FILE): return {}
    try:
        with open(IP_CACHE_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_ip_cache(data):
    try:
        with open(IP_CACHE_FILE, 'w') as f: json.dump(data, f, indent=4)
    except: pass

# --- 数据平滑迁移模块 ---
def load_permissions():
    if not os.path.exists(PERMS_FILE): return {}
    try:
        with open(PERMS_FILE, 'r') as f:
            perms = json.load(f)
            
        # [平滑升级逻辑]：如果发现旧版本的一维列表数据，自动转为包含到期时间的新版二维字典
        migrated = False
        for uid, data in perms.items():
            if 'ocids' in data and isinstance(data['ocids'], list):
                old_expire = data.get('expire_time', '')
                new_ocids_dict = {}
                for ocid in data['ocids']:
                    new_ocids_dict[ocid] = old_expire  # 把全局过期时间赋给每台机器
                data['ocids'] = new_ocids_dict
                if 'expire_time' in data: del data['expire_time']
                migrated = True
                
        if migrated: save_permissions(perms)
        return perms
    except Exception as e:
        print(f"解析权限文件出错: {e}")
        return {}

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
        "time": get_bj_now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id, "server": server_name, "old_ip": old_ip, "new_ip": new_ip
    })
    with open(STATS_FILE, 'w') as f: json.dump(stats, f, indent=4)
    return stats["total_changes"]

def send_tg_message(chat_id, text):
    if not chat_id: return
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    except: pass

def verify_admin(req_data):
    if not req_data: return False
    req_code = req_data.get('code')
    actual_code = admin_session.get('code')
    expires = admin_session.get('expires', 0)
    
    if not req_code or not actual_code: return False
    if admin_session.get('attempts', 0) >= 5: return False
    if time.time() > expires: return False
        
    if secrets.compare_digest(str(req_code), str(actual_code)):
        admin_session['attempts'] = 0
        return True
    else:
        admin_session['attempts'] += 1
        return False

# ==========================================
# OCI 核心逻辑 (多账号适配 + IP缓存获取)
# ==========================================
def load_oci_accounts():
    accounts = {}
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                accounts = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"读取 {ACCOUNTS_FILE} 失败: {e}")
    if not accounts and os.getenv('OCI_USER_OCID'):
        accounts["默认账号"] = {
            "user": os.getenv('OCI_USER_OCID'),
            "key_file": os.getenv('OCI_KEY_FILE'),
            "fingerprint": os.getenv('OCI_FINGERPRINT'),
            "tenancy": os.getenv('OCI_TENANCY_OCID'),
            "region": os.getenv('OCI_REGION')
        }
    return accounts

def fetch_oci_instances():
    global servers, instance_config_map
    try:
        accounts = load_oci_accounts()
        new_servers, new_config_map, error_msgs = {}, {}, []
        
        for acc_name, config in accounts.items():
            try:
                compute_client = oci.core.ComputeClient(config)
                data = compute_client.list_instances(compartment_id=config["tenancy"]).data
                for i in data:
                    if i.lifecycle_state == 'RUNNING':
                        display_name = f"[{acc_name}] {i.display_name}"
                        new_servers[display_name] = i.id
                        new_config_map[i.id] = config
            except Exception as e:
                error_msgs.append(f"{acc_name} 错误: {str(e)}")
        
        servers, instance_config_map = new_servers, new_config_map
        if error_msgs: return False, "部分同步失败: " + " | ".join(error_msgs)
        return True, "同步成功"
    except Exception as e: return False, str(e)

# 不换IP，仅查询当前公网IP (避免影响正常环境，仅做展示)
def get_instance_public_ip_safe(target_ocid):
    config = instance_config_map.get(target_ocid)
    if not config: return None
    try:
        compute_client = oci.core.ComputeClient(config)
        vnc_client = oci.core.VirtualNetworkClient(config)
        vnic_attach = compute_client.list_vnic_attachments(compartment_id=config["tenancy"], instance_id=target_ocid).data
        if not vnic_attach: return None
        vnic_id = vnic_attach[0].vnic_id
        
        p_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        if not p_ips: return None
        
        get_details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=p_ips[0].id)
        pub_ip = vnc_client.get_public_ip_by_private_ip_id(get_details).data
        return pub_ip.ip_address
    except Exception: return None

# 智能获取IP：优先读缓存，没有则实时查并写入缓存
def get_or_fetch_ip(ocid):
    cache = load_ip_cache()
    if ocid in cache: return cache[ocid]
    
    real_ip = get_instance_public_ip_safe(ocid)
    if real_ip:
        cache[ocid] = real_ip
        save_ip_cache(cache)
        return real_ip
    return "未知 IP"

def change_oracle_ip(target_ocid):
    config = instance_config_map.get(target_ocid)
    if not config: return None, None
        
    try:
        compute_client = oci.core.ComputeClient(config)
        vnc_client = oci.core.VirtualNetworkClient(config)
        vnic_attach = compute_client.list_vnic_attachments(compartment_id=config["tenancy"], instance_id=target_ocid).data
        if not vnic_attach: raise Exception("未找到网卡")
        vnic_id = vnic_attach[0].vnic_id
        
        p_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        if not p_ips: raise Exception("未找到内网IP")
        p_ip_id = p_ips[0].id

        old_ip = "Unknown"
        try:
            get_details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=p_ip_id)
            pub_ip = vnc_client.get_public_ip_by_private_ip_id(get_details).data
            old_ip = pub_ip.ip_address
            if pub_ip.lifetime == 'RESERVED':
                vnc_client.update_public_ip(pub_ip.id, oci.core.models.UpdatePublicIpDetails(private_ip_id=""))
            else:
                vnc_client.delete_public_ip(pub_ip.id)
            time.sleep(2)
        except oci.exceptions.ServiceError as e:
            if e.status == 404: old_ip = "None"
            else: raise e

        create_info = oci.core.models.CreatePublicIpDetails(compartment_id=config["tenancy"], lifetime="EPHEMERAL", private_ip_id=p_ip_id)
        new_ip = vnc_client.create_public_ip(create_info).data.ip_address
        
        # 换IP成功后，更新缓存
        cache = load_ip_cache()
        cache[target_ocid] = new_ip
        save_ip_cache(cache)
        
        return old_ip, new_ip
    except Exception as e:
        send_tg_message(ADMIN_ID, f"⚠️ **OCI 报错**\n\n`{str(e)}`\n\n实例: `{target_ocid}`")
        return None, None

fetch_oci_instances()

# ==========================================
# Telegram 机器人交互端
# ==========================================
def is_whitelisted(user_id):
    user_id = str(user_id)
    return user_id == str(ADMIN_ID) or user_id in load_permissions()

@bot.message_handler(commands=['list'])
def admin_list_users(message):
    uid = str(message.chat.id)
    if uid != str(ADMIN_ID): return bot.send_message(uid, "⛔ **权限拒绝**\n此命令仅限超级管理员使用。")

    perms = load_permissions()
    if not perms: return bot.send_message(uid, "📝 当前系统没有任何客户数据。")

    msg = "📋 **客户授权目录**\n\n"
    for user_tg_id, data in perms.items():
        max_c = data.get('max_changes', 0)
        used_c = data.get('used_changes', 0)
        rem = max(max_c - used_c, 0)
        ocids_dict = data.get('ocids', {})
        
        msg += f"👤 **客户 ID**: `{user_tg_id}`\n📊 **总剩余额度**: `{rem}` 次\n"
        if not ocids_dict:
            msg += "🖥️ **名下机器**: `未分配`\n"
        else:
            for ocid, exp in ocids_dict.items():
                s_name = next((n for n, o in servers.items() if o == ocid), "未知节点")
                exp_display = exp if exp else "永久有效"
                msg += f" ├ 🖥️ {s_name} (到期: `{exp_display}`)\n"
        msg += "➖" * 12 + "\n"

    for x in range(0, len(msg), 4000): bot.send_message(uid, msg[x:x+4000], parse_mode="Markdown")

@bot.message_handler(commands=['start', 'menu'])
def user_menu(message):
    if not is_whitelisted(message.chat.id): return
    uid = str(message.chat.id)
    perms = load_permissions().get(uid, {})
    ocids_dict = perms.get('ocids', {}) # 新格式：字典 {ocid: expire_time}

    if not ocids_dict and uid != str(ADMIN_ID): 
        return bot.send_message(uid, "❌ 您暂无可用服务器授权")

    max_c = perms.get('max_changes', 0)
    used_c = perms.get('used_changes', 0)
    rem = max(max_c - used_c, 0)

    # 1. 立即发送加载提示（因为查询缺失IP可能会卡顿1-2秒）
    loading_msg = bot.send_message(uid, "⏳ 正在为您拉取专属控制台数据，请稍候...", parse_mode="Markdown")

    # 2. 构建美化的交互面板
    now_dt = get_bj_now()
    msg_text = f"🎛️ **您的专属 OCI 控制台**\n📊 当前总剩余额度：`{rem}` 次\n\n"
    markup = InlineKeyboardMarkup()
    
    has_valid_server = False

    for ocid, exp_str in ocids_dict.items():
        s_name = next((n for n, id in servers.items() if id == ocid), "未知节点")
        
        # 验证单机台是否过期
        is_expired = False
        days_left_text = ""
        if exp_str:
            try:
                exp_dt = datetime.strptime(exp_str + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ_TZ)
                if now_dt > exp_dt: is_expired = True
                else: days_left_text = f" (剩余 {(exp_dt.date() - now_dt.date()).days} 天)"
            except: pass
            
        exp_display = f"`{exp_str}`{days_left_text}" if exp_str else "`永久有效`"
        
        msg_text += f"🖥️ **节点：{s_name}**\n"
        if is_expired:
            msg_text += f"⛔ 状态：`已到期停用` (原到期: {exp_str})\n\n"
        else:
            has_valid_server = True
            
            # 智能获取 IP (读缓存优先)
            current_ip = get_or_fetch_ip(ocid)
            msg_text += f"🌐 当前IP：`{current_ip}`\n"
            msg_text += f"📅 到期：{exp_display}\n\n"
            
            markup.add(InlineKeyboardButton(f"🔄 换IP | {current_ip}", callback_data=f"ip_{get_short_id(ocid)}"))

    if not has_valid_server and uid != str(ADMIN_ID):
        msg_text += "\n⚠️ 您名下的所有服务器均已到期，无法进行操作。请联系管理员续费。"

    if rem <= 0 and uid != str(ADMIN_ID):
        msg_text += "\n⚠️ **您的换 IP 额度已耗尽，请联系管理员充值。**"

    # 3. 覆盖掉加载提示，展示最终界面
    bot.edit_message_text(text=msg_text, chat_id=uid, message_id=loading_msg.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ip_'))
def handle_change_ip(call):
    if not is_whitelisted(call.message.chat.id):
        return bot.answer_callback_query(call.id, "⛔ 非法请求", show_alert=True)

    uid = str(call.message.chat.id)
    short_id = call.data[3:] 
    
    target_ocid = next((o for n, o in servers.items() if get_short_id(o) == short_id), None)
    if not target_ocid: return bot.answer_callback_query(call.id, "❌ 找不到对应的实例，可能已被删除！", show_alert=True)

    perms = load_permissions()
    user_data = perms.get(uid, {})
    ocids_dict = user_data.get('ocids', {})
    
    if target_ocid not in ocids_dict: return bot.answer_callback_query(call.id, "❌ 授权已撤销！", show_alert=True)
    
    # 点击时再次严格校验单机台过期情况
    exp_str = ocids_dict.get(target_ocid, '')
    if exp_str:
        try:
            exp_dt = datetime.strptime(exp_str + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ_TZ)
            if get_bj_now() > exp_dt:
                return bot.answer_callback_query(call.id, "❌ 该节点服务已到期！", show_alert=True)
        except: pass

    max_changes = user_data.get('max_changes', 0)
    used_changes = user_data.get('used_changes', 0)
    if used_changes >= max_changes and uid != str(ADMIN_ID):
        bot.answer_callback_query(call.id, "❌ 额度已用完！", show_alert=True)
        return
    
    server_name = next((n for n, o in servers.items() if o == target_ocid), "未知节点")
    bot.edit_message_text(f"⏳ 正在向甲骨文发送更换指令，请耐心等待 (约需10~20秒)...", 
                          chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
    
    old_ip, new_ip = change_oracle_ip(target_ocid)
    rem = max(max_changes - used_changes, 0)
    
    if new_ip:
        perms[uid]['used_changes'] += 1
        save_permissions(perms)
        rem = max(max_changes - perms[uid]['used_changes'], 0)
        
        bot.edit_message_text(f"✅ **IP 更换成功！**\n\n🖥️ 节点: `{server_name}`\n🌐 新 IP: `{new_ip}`\n📊 剩余额度: `{rem}` 次", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        
        send_tg_message(ADMIN_ID, f"🟢 **客户换IP (成功)**\n👤 客户 ID: `{uid}`\n🖥️ 节点: `{server_name}`\n🌐 新 IP: `{new_ip}`\n💳 剩余额度: `{rem}` 次")
    else:
        bot.edit_message_text("❌ 更换失败 (API 限制)。\n**本次操作不扣除额度**，请稍后再试。", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id)
        send_tg_message(ADMIN_ID, f"🔴 **客户换IP (失败)**\n👤 客户 ID: `{uid}`\n🖥️ 节点: `{server_name}`\n❌ 原因: `调用失败或频繁`\n💡 本次操作未扣除额度。")

# ==========================================
# 定时提醒系统 (独立单节点检测)
# ==========================================
def reminder_loop():
    last_check_date = None
    while True:
        try:
            now = get_bj_now()
            today_str = now.strftime("%Y-%m-%d")
            
            if last_check_date != today_str and now.hour >= 12:
                perms = load_permissions()
                for uid, data in perms.items():
                    ocids_dict = data.get('ocids', {})
                    for ocid, exp_str in ocids_dict.items():
                        if not exp_str: continue
                        try:
                            s_name = next((n for n, id in servers.items() if id == ocid), "未知节点")
                            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                            days_left = (exp_date - now.date()).days
                            
                            if days_left in [6, 4, 2]:
                                msg = f"⏳ **服务续费提醒**\n\n您的节点 `{s_name}` 距离到期仅剩 `{days_left}` 天。\n📅 到期时间：`{exp_str}`\n请及时联系管理员续期，以免影响正常使用。"
                                send_tg_message(uid, msg)
                                send_tg_message(ADMIN_ID, f"🔔 **续费预警**\n客户 ID: `{uid}`\n机器: `{s_name}`\n剩余 `{days_left}` 天。")
                            elif days_left == 0:
                                send_tg_message(uid, f"⚠️ **服务今日到期**\n\n您的节点 `{s_name}` 将于**今天 23:59** 到期，请尽快续费！")
                                send_tg_message(ADMIN_ID, f"🔴 **客户今日到期**\n客户 ID: `{uid}`\n机器: `{s_name}`，请准备停机或跟进。")
                        except Exception: pass
                last_check_date = today_str
        except Exception: pass
        time.sleep(3600)

threading.Thread(target=lambda: bot.infinity_polling(timeout=20), daemon=True).start()
threading.Thread(target=reminder_loop, daemon=True).start()

# ==========================================
# Flask Web API
# ==========================================
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/admin/send-code', methods=['POST'])
def send_code():
    code = str(random.randint(100000, 999999))
    admin_session.update({"code": code, "expires": time.time() + 3600, "attempts": 0})
    send_tg_message(ADMIN_ID, f"🔐 后台验证码：`{code}`")
    return jsonify({"success": True})

@app.route('/api/admin/data', methods=['POST'])
def get_data():
    if not verify_admin(request.json): 
        return jsonify({"success": False, "error": "验证失败、已过期或尝试次数过多"})
    return jsonify({"success": True, "servers": servers, "permissions": load_permissions()})

@app.route('/api/admin/sync', methods=['POST'])
def sync_data():
    if not verify_admin(request.json):
        return jsonify({"success": False, "error": "验证失败"})
    success, msg = fetch_oci_instances()
    return jsonify({"success": success, "servers": servers, "error": msg if not success else ""})

@app.route('/api/admin/save', methods=['POST'])
def save_data():
    d = request.json
    if not verify_admin(d): return jsonify({"success": False, "error": "验证失败"})
    
    uid = str(d.get('tg_id', '')).strip()
    if not uid: return jsonify({"success": False, "error": "TG ID 不能为空"})
    
    # 接收新格式的 ocids_dict: {"ocid1": "2026-12-31", ...}
    ocids_dict = d.get('ocids', {})
    
    p = load_permissions()
    p[uid] = {
        "ocids": ocids_dict,
        "max_changes": int(d.get('max_changes', 0)),
        "used_changes": p.get(uid, {}).get('used_changes', 0)
        # 移除了全局的 expire_time
    }
    save_permissions(p)
    return jsonify({"success": True})

@app.route('/api/admin/delete', methods=['POST'])
def delete_data():
    d = request.json
    if not verify_admin(d): return jsonify({"success": False})
    uid = str(d.get('tg_id', '')).strip()
    p = load_permissions()
    if uid in p:
        del p[uid]
        save_permissions(p)
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
