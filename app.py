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

ACCOUNTS_FILE = 'oci_accounts.yaml'

def load_full_yaml():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"读取 {ACCOUNTS_FILE} 失败: {e}")
    return {}

_init_config = load_full_yaml()

TG_BOT_TOKEN = str(_init_config.get('bot_token', os.getenv('TG_BOT_TOKEN', '')))
ADMIN_ID = str(_init_config.get('admin_id', os.getenv('ADMIN_TG_ID', '')))

STATS_FILE = 'stats.json'
PERMS_FILE = 'permissions.json'
IP_CACHE_FILE = 'ip_cache.json'
TRAFFIC_CACHE_FILE = 'traffic_cache.json'
TRAFFIC_LIMITS_FILE = 'traffic_limits.json' 

BJ_TZ = timezone(timedelta(hours=8))

# 核心数据结构: ocid -> {name, state, account}
all_instances = {}
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

def load_json_cache(filename):
    if not os.path.exists(filename): return {}
    try:
        with open(filename, 'r') as f: return json.load(f)
    except: return {}

def save_json_cache(filename, data):
    try:
        with open(filename, 'w') as f: json.dump(data, f, indent=4)
    except: pass

def load_permissions():
    perms = load_json_cache(PERMS_FILE)
    migrated = False
    for uid, data in perms.items():
        if 'ocids' in data and isinstance(data['ocids'], list):
            old_expire = data.get('expire_time', '')
            new_ocids_dict = {ocid: old_expire for ocid in data['ocids']}
            data['ocids'] = new_ocids_dict
            if 'expire_time' in data: del data['expire_time']
            migrated = True
    if migrated: save_permissions(perms)
    return perms

def save_permissions(data):
    save_json_cache(PERMS_FILE, data)

def log_change(user_id, server_name, old_ip, new_ip):
    stats = load_json_cache(STATS_FILE)
    if "total_changes" not in stats: stats["total_changes"] = 0
    if "history" not in stats: stats["history"] = []
    
    stats["total_changes"] += 1
    stats["history"].append({
        "time": get_bj_now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id, "server": server_name, "old_ip": old_ip, "new_ip": new_ip
    })
    save_json_cache(STATS_FILE, stats)
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
# OCI 核心逻辑 & 流量抓取 
# ==========================================
def load_oci_accounts():
    config = load_full_yaml()
    accounts = {}
    for k, v in config.items():
        if isinstance(v, dict):
            accounts[k] = v
            
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
    global all_instances, instance_config_map
    try:
        accounts = load_oci_accounts()
        new_all_instances, new_config_map, error_msgs = {}, {}, []
        
        for acc_name, config in accounts.items():
            try:
                compute_client = oci.core.ComputeClient(config)
                data = compute_client.list_instances(compartment_id=config["tenancy"]).data
                for i in data:
                    if i.lifecycle_state in ['TERMINATED', 'TERMINATING']: 
                        continue
                    
                    display_name = f"[{acc_name}] {i.display_name}"
                    new_config_map[i.id] = config
                    new_all_instances[i.id] = {
                        "name": display_name,
                        "state": i.lifecycle_state,
                        "account": acc_name
                    }
            except Exception as e:
                error_msgs.append(f"{acc_name} 错误: {str(e)}")
        
        all_instances = new_all_instances
        instance_config_map = new_config_map
        if error_msgs: return False, "部分同步失败: " + " | ".join(error_msgs)
        return True, "同步成功"
    except Exception as e: return False, str(e)

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

def get_or_fetch_ip(ocid):
    cache = load_json_cache(IP_CACHE_FILE)
    if ocid in cache: return cache[ocid]
    
    real_ip = get_instance_public_ip_safe(ocid)
    if real_ip:
        cache[ocid] = real_ip
        save_json_cache(IP_CACHE_FILE, cache)
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
        
        cache = load_json_cache(IP_CACHE_FILE)
        cache[target_ocid] = new_ip
        save_json_cache(IP_CACHE_FILE, cache)
        
        return old_ip, new_ip
    except Exception as e:
        send_tg_message(ADMIN_ID, f"⚠️ **OCI 报错**\n\n`{str(e)}`\n\n实例: `{target_ocid}`")
        return None, None

def fetch_traffic_for_account(config):
    try:
        monitoring_client = oci.monitoring.MonitoringClient(config)
        now_utc = datetime.now(timezone.utc)
        
        start_time = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_time_str = start_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        end_time_str = now_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        response = monitoring_client.summarize_metrics_data(
            compartment_id=config["tenancy"],
            summarize_metrics_data_details=oci.monitoring.models.SummarizeMetricsDataDetails(
                namespace="oci_vcn",
                query="VnicToNetworkBytes[1h].sum()", 
                start_time=start_time_str,
                end_time=end_time_str
            ),
            compartment_id_in_subtree=True
        )
        total_bytes = 0
        for item in response.data:
            for dp in item.aggregated_datapoints:
                total_bytes += dp.value
        return total_bytes / (1024**3) 
    except Exception as e:
        print(f"Traffic Fetch Error for {config.get('region', 'Unknown')}: {e}")
        return -1

def suspend_account_instances(acc_name, config):
    try:
        compute_client = oci.core.ComputeClient(config)
        instances = compute_client.list_instances(compartment_id=config["tenancy"]).data
        stopped_count = 0
        stopped_names = []
        
        for i in instances:
            if i.lifecycle_state == 'RUNNING':
                compute_client.instance_action(i.id, "SOFTSTOP")
                stopped_count += 1
                stopped_names.append(i.display_name)
                time.sleep(1) 
                
        return stopped_count, stopped_names
    except Exception as e:
        print(f"Suspend Error for {acc_name}: {e}")
        return 0, []

# ==========================================
# 后台定时任务：自动熔断 & 每日战报
# ==========================================
def background_jobs_loop():
    last_report_day = None
    last_traffic_report_utc = None
    last_traffic_check_utc = None
    high_frequency_mode = False
    
    while True:
        try:
            now_bj = get_bj_now()
            today_bj_str = now_bj.strftime("%Y-%m-%d")
            
            now_utc = datetime.now(timezone.utc)
            today_utc_str = now_utc.strftime("%Y-%m-%d")
            
            # --- 任务 1：每日中午 12 点发送节点到期提醒 ---
            if last_report_day != today_bj_str and now_bj.hour >= 12:
                perms = load_permissions()
                for uid, data in perms.items():
                    ocids_dict = data.get('ocids', {})
                    for ocid, exp_str in ocids_dict.items():
                        if not exp_str: continue
                        try:
                            s_name = all_instances.get(ocid, {}).get("name", "未知节点")
                            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                            days_left = (exp_date - now_bj.date()).days
                            
                            if days_left in [6, 4, 2]:
                                send_tg_message(uid, f"⏳ **服务续费提醒**\n\n您的节点 `{s_name}` 距离到期仅剩 `{days_left}` 天。\n📅 到期时间：`{exp_str}`\n请及时联系管理员续期，以免影响正常使用。")
                                send_tg_message(ADMIN_ID, f"🔔 **续费预警**\n客户 ID: `{uid}`\n机器: `{s_name}`\n剩余 `{days_left}` 天。")
                            elif days_left == 0:
                                send_tg_message(uid, f"⚠️ **服务今日到期**\n\n您的节点 `{s_name}` 将于**今天 23:59** 到期，请尽快续费！")
                                send_tg_message(ADMIN_ID, f"🔴 **客户今日到期**\n客户 ID: `{uid}`\n机器: `{s_name}`，请准备停机或跟进。")
                        except Exception: pass
                last_report_day = today_bj_str
                
            # --- 任务 2：智能流量检测 (常规每日一查，超90%后自动切为每小时一查) ---
            need_check = False
            if last_traffic_check_utc != today_utc_str:
                need_check = True
            elif high_frequency_mode:
                need_check = True

            if need_check:
                accounts = load_oci_accounts()
                limits_data = load_json_cache(TRAFFIC_LIMITS_FILE)
                cache = load_json_cache(TRAFFIC_CACHE_FILE)
                any_at_risk = False
                
                for acc_name, acc_conf in accounts.items():
                    limit = int(limits_data.get(acc_name, 0))
                    usage_gb = fetch_traffic_for_account(acc_conf)
                    
                    if usage_gb >= 0:
                        cache[acc_name] = {"usage_gb": usage_gb, "update_time": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")}
                        if limit > 0:
                            percent = (usage_gb / limit) * 100
                            if percent >= 90:
                                any_at_risk = True
                            
                            # 🔥 熔断核心逻辑
                            if usage_gb >= limit:
                                stopped_count, stopped_names = suspend_account_instances(acc_name, acc_conf)
                                if stopped_count > 0:
                                    fetch_oci_instances() # 刷新状态
                                    send_tg_message(ADMIN_ID, f"🛑 **自动熔断触发**\n\n账号: `{acc_name}`\n当前用量: `{usage_gb:.2f} GB` (限额 `{limit} GB`)\n\n系统已为您**强制关机**以下 {stopped_count} 台服务器以阻断网络出站及扣费：\n`{', '.join(stopped_names)}`\n\n*(如需恢复，请下个月前往甲骨文官网或TG后台手动开机)*")
                                    
                high_frequency_mode = any_at_risk
                last_traffic_check_utc = today_utc_str
                save_json_cache(TRAFFIC_CACHE_FILE, cache)
            
            # --- 任务 3：每日 UTC 00:00 (北京时间 08:00) 发送战报 ---
            if last_traffic_report_utc != today_utc_str:
                accounts = load_oci_accounts()
                limits_data = load_json_cache(TRAFFIC_LIMITS_FILE)
                cache = load_json_cache(TRAFFIC_CACHE_FILE)
                report_lines = []
                
                for acc_name, acc_conf in accounts.items():
                    limit = int(limits_data.get(acc_name, 0))
                    info = cache.get(acc_name, {})
                    usage_gb = info.get("usage_gb", -1)
                    
                    if usage_gb >= 0:
                        if limit > 0:
                            percent = (usage_gb / limit) * 100
                            alert_icon = "🔴" if percent >= 100 else ("🟡" if percent >= 70 else "🟢")
                            report_lines.append(f"{alert_icon} **{acc_name}**\n用量: `{usage_gb:.2f} GB` / `{limit} GB` ({percent:.1f}%)")
                        else:
                            report_lines.append(f"🟢 **{acc_name}**\n用量: `{usage_gb:.2f} GB` / `不限`")
                    else:
                        limit_text = f"{limit} GB" if limit > 0 else "不限"
                        report_lines.append(f"❓ **{acc_name}**\n用量: `获取失败` / `{limit_text}`")
                
                report_msg = f"📊 **每日出站流量战报 (基于 UTC 月初)**\n📅 UTC 日期: `{today_utc_str}`\n*(每个自然月1号系统自动从零计算)*\n\n" + "\n\n".join(report_lines)
                send_tg_message(ADMIN_ID, report_msg)
                
                last_traffic_report_utc = today_utc_str

        except Exception as e:
            print(f"Background Loop Error: {e}")
            
        time.sleep(3600)

# ==========================================
# Telegram 机器人客户端交互逻辑
# ==========================================
def is_whitelisted(user_id):
    user_id = str(user_id)
    if user_id == str(ADMIN_ID): return True 
    perms = load_permissions()
    if user_id in perms: return True
    return False

@bot.message_handler(commands=['list'])
def admin_list_users(message):
    uid = str(message.chat.id)
    if uid != str(ADMIN_ID): return bot.send_message(uid, "⛔ **权限拒绝**\n此命令仅限超级管理员使用。")

    # === 第一部分：客户授权目录 ===
    perms = load_permissions()
    msg = "📋 **客户授权目录**\n\n"
    if not perms:
        msg += "📝 当前系统没有任何客户数据。\n"
    else:
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
                    info = all_instances.get(ocid, {})
                    s_name = info.get("name", "未知节点 (可能已删除)")
                    exp_display = exp if exp else "永久有效"
                    msg += f" ├ 🖥️ {s_name} (到期: `{exp_display}`)\n"
            msg += "➖" * 12 + "\n"

    # === 第二部分：API 账号当月出站流量概览 ===
    msg += "\n📈 **当月出站流量概览 (读取最新缓存)**\n\n"
    accounts = load_oci_accounts()
    limits_data = load_json_cache(TRAFFIC_LIMITS_FILE)
    traffic_cache = load_json_cache(TRAFFIC_CACHE_FILE)
    
    if not accounts:
        msg += "⚠️ 暂无 API 账号配置。\n"
    else:
        for acc_name in accounts.keys():
            limit = int(limits_data.get(acc_name, 0))
            info = traffic_cache.get(acc_name, {})
            usage_gb = info.get("usage_gb", -1)
            
            if usage_gb >= 0:
                if limit > 0:
                    percent = (usage_gb / limit) * 100
                    alert_icon = "🔴" if percent >= 100 else ("🟡" if percent >= 70 else "🟢")
                    msg += f"{alert_icon} **{acc_name}**\n   用量: `{usage_gb:.2f} GB` / `{limit} GB` ({percent:.1f}%)\n"
                else:
                    msg += f"🟢 **{acc_name}**\n   用量: `{usage_gb:.2f} GB` / `不限`\n"
            else:
                limit_text = f"{limit} GB" if limit > 0 else "不限"
                msg += f"❓ **{acc_name}**\n   用量: `获取失败或同步中` / `{limit_text}`\n"

    # 发送消息 (如果消息太长会自动切分)
    for x in range(0, len(msg), 4000): 
        bot.send_message(uid, msg[x:x+4000], parse_mode="Markdown")

@bot.message_handler(commands=['start', 'menu'])
def user_menu(message):
    if not is_whitelisted(message.chat.id): return

    user_id = str(message.chat.id)
    perms = load_permissions()
    user_data = perms.get(user_id, {})
    ocids_dict = user_data.get('ocids', {})
    max_changes = user_data.get('max_changes', 0)
    used_changes = user_data.get('used_changes', 0)

    if not ocids_dict and user_id != str(ADMIN_ID):
        bot.send_message(message.chat.id, "❌ **权限拒绝**\n您当前没有任何授权可操作的服务器。")
        return
        
    remaining = max_changes - used_changes
    loading_msg = bot.send_message(user_id, "⏳ 正在拉取面板信息，请稍候...", parse_mode="Markdown")

    now_dt = get_bj_now()
    msg_text = f"🎛️ **您的专属 OCI 控制台**\n📊 当前剩余额度：`{remaining}` 次\n\n"
    markup = InlineKeyboardMarkup()
    has_valid_server = False

    for ocid, exp_str in ocids_dict.items():
        info = all_instances.get(ocid, {})
        s_name = info.get("name", "未知节点 (可能已删除)")
        state = info.get("state", "UNKNOWN")
        
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
        
        if state == 'STOPPED':
            msg_text += f"⛔ 状态：`系统已关机 (可能因流量耗尽或管理员停机)`\n\n"
        elif is_expired:
            msg_text += f"⛔ 状态：`已到期停用` (原到期: {exp_str})\n\n"
        elif state == 'RUNNING':
            has_valid_server = True
            current_ip = get_or_fetch_ip(ocid)
            msg_text += f"🌐 当前IP：`{current_ip}`\n"
            msg_text += f"📅 到期：{exp_display}\n\n"
            markup.add(InlineKeyboardButton(f"🔄 换IP | {current_ip}", callback_data=f"ip_{get_short_id(ocid)}"))
        else:
            msg_text += f"⚠️ 状态：`当前无法操作 ({state})`\n\n"

    if not has_valid_server and user_id != str(ADMIN_ID):
        msg_text += "\n⚠️ 您名下的所有服务器均不可用 (已关机或已到期)。"

    if remaining <= 0 and user_id != str(ADMIN_ID):
        msg_text += "\n⚠️ **您的额度已耗尽，请联系管理员充值。**"

    bot.edit_message_text(text=msg_text, chat_id=user_id, message_id=loading_msg.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ip_'))
def handle_change_ip(call):
    if not is_whitelisted(call.message.chat.id):
        bot.answer_callback_query(call.id, "⛔ 非法请求，账号未受信任！", show_alert=True)
        return

    user_id = str(call.message.chat.id)
    short_id = call.data[3:] 
    
    target_ocid = None
    for ocid in all_instances:
        if get_short_id(ocid) == short_id:
            target_ocid = ocid
            break
            
    if not target_ocid:
        bot.answer_callback_query(call.id, "❌ 找不到对应的服务器实例，可能已被删除！", show_alert=True)
        return

    # 拦截未运行的机器
    state = all_instances.get(target_ocid, {}).get("state", "UNKNOWN")
    if state != "RUNNING":
        bot.answer_callback_query(call.id, "❌ 该节点已关机，无法更换IP！请联系管理员。", show_alert=True)
        return

    perms = load_permissions()
    user_data = perms.get(user_id, {})
    ocids_dict = user_data.get('ocids', {})
    
    if target_ocid not in ocids_dict:
        bot.answer_callback_query(call.id, "❌ 授权已过期或被撤销！", show_alert=True)
        return
        
    exp_str = ocids_dict.get(target_ocid, '')
    if exp_str:
        try:
            exp_dt = datetime.strptime(exp_str + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ_TZ)
            if get_bj_now() > exp_dt:
                return bot.answer_callback_query(call.id, "❌ 该节点服务已到期！", show_alert=True)
        except: pass

    max_changes = user_data.get('max_changes', 0)
    used_changes = user_data.get('used_changes', 0)
    if used_changes >= max_changes and user_id != str(ADMIN_ID):
        bot.answer_callback_query(call.id, "❌ 额度已用完！", show_alert=True)
        return
    
    server_name = all_instances.get(target_ocid, {}).get("name", "未知节点")
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
        
        send_tg_message(ADMIN_ID, f"🟢 **客户换IP (成功)**\n\n👤 客户 ID: `{user_id}`\n🖥️ 节点: `{server_name}`\n🔄 旧 IP: `{old_ip}`\n🌐 新 IP: `{new_ip}`\n💳 剩余额度: `{remaining}`\n📊 系统总更换: `{count}`")
    else:
        bot.edit_message_text("❌ 更换失败 (API抽风或频率限制)。\n**本次操作不扣除您的额度**，请稍后再试。", 
                              chat_id=call.message.chat.id, message_id=call.message.message_id)
        send_tg_message(ADMIN_ID, f"🔴 **客户换IP (失败)**\n\n👤 客户 ID: `{user_id}`\n🖥️ 节点: `{server_name}`\n❌ 原因: `甲骨文API拒绝或调用频繁`\n💡 本次操作未扣除客户额度。")

def run_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception:
            time.sleep(3)

fetch_oci_instances()
threading.Thread(target=run_bot_polling, daemon=True).start()
threading.Thread(target=background_jobs_loop, daemon=True).start()

# ==========================================
# 网页管理后台端 (Web API)
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
    code = str(random.randint(100000, 999999))
    admin_session["code"] = code
    admin_session["expires"] = time.time() + 3600 

    send_tg_message(ADMIN_ID, f"🔐 **后台登录验证码**\n\n您的动态密码为：`{code}`\n\n该验证码在 1 小时内有效。")
    return jsonify({"success": True})

@app.route('/api/admin/data', methods=['POST'])
def admin_data():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误或已过期"})
    return jsonify({"success": True, "instances": all_instances, "permissions": load_permissions()})

@app.route('/api/admin/sync', methods=['POST'])
def admin_sync():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误或已过期"})
    success, msg = fetch_oci_instances()
    return jsonify({"success": success, "message": msg, "instances": all_instances})

# --- 新增：电源启停操作 API ---
@app.route('/api/admin/instance-action', methods=['POST'])
def admin_instance_action():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误"})
    ocid = request.json.get('ocid')
    action = request.json.get('action') # 'START' or 'SOFTSTOP'
    
    config = instance_config_map.get(ocid)
    if not config: return jsonify({"success": False, "error": "找不到该实例配置"})
    
    try:
        compute_client = oci.core.ComputeClient(config)
        compute_client.instance_action(ocid, action)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/admin/set-traffic-limit', methods=['POST'])
def set_traffic_limit():
    if not check_auth(request): return jsonify({"success": False, "error": "验证失败"})
    acc_name = request.json.get('account')
    limit_gb = max(0, int(request.json.get('limit_gb', 0)))
    
    limits = load_json_cache(TRAFFIC_LIMITS_FILE)
    limits[acc_name] = limit_gb
    save_json_cache(TRAFFIC_LIMITS_FILE, limits)
    return jsonify({"success": True})

@app.route('/api/admin/traffic', methods=['POST'])
def admin_traffic():
    if not check_auth(request): return jsonify({"success": False, "error": "验证失败"})
    
    force_refresh = request.json.get('force', False)
    cache = load_json_cache(TRAFFIC_CACHE_FILE)
    limits_data = load_json_cache(TRAFFIC_LIMITS_FILE)
    accounts = load_oci_accounts()
    
    res_data = {}
    for acc_name, acc_conf in accounts.items():
        limit = int(limits_data.get(acc_name, 0)) 
        if force_refresh or acc_name not in cache:
            usage = fetch_traffic_for_account(acc_conf)
            if usage >= 0:
                cache[acc_name] = {
                    "usage_gb": usage, 
                    "update_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                }
                
                # 🔥 网页端强刷如果发现超额，也立即执行熔断保护
                if limit > 0 and usage >= limit:
                    stopped_count, stopped_names = suspend_account_instances(acc_name, acc_conf)
                    if stopped_count > 0:
                        fetch_oci_instances()
                        send_tg_message(ADMIN_ID, f"🛑 **手动强刷触发熔断**\n\n账号: `{acc_name}`\n当前用量: `{usage:.2f} GB` (超额)\n已为您强制关机保护 {stopped_count} 台实例！")
        
        usage = cache.get(acc_name, {}).get("usage_gb", -1)
        update_time = cache.get(acc_name, {}).get("update_time", "从未同步")
        res_data[acc_name] = {"usage_gb": usage, "limit_gb": limit, "update_time": update_time}
    
    if force_refresh: save_json_cache(TRAFFIC_CACHE_FILE, cache)
    return jsonify({"success": True, "traffic": res_data})

@app.route('/api/admin/save', methods=['POST'])
def admin_save():
    if not check_auth(request): return jsonify({"success": False, "error": "验证码错误或已过期"})
    
    data = request.json
    target_tg_id = str(data.get('tg_id', '')).strip()
    selected_ocids = data.get('ocids', {})
    max_changes = int(data.get('max_changes', 0))
    
    if not target_tg_id: return jsonify({"success": False, "error": "缺少用户ID"})

    perms = load_permissions()
    used_changes = perms.get(target_tg_id, {}).get('used_changes', 0)
    perms[target_tg_id] = {
        "ocids": selected_ocids,
        "max_changes": max_changes,
        "used_changes": used_changes
    }
        
    save_permissions(perms)
    return jsonify({"success": True})

@app.route('/api/admin/delete', methods=['POST'])
def admin_delete():
    if not check_auth(request): return jsonify({"success": False, "error": "验证失败"})
    target_tg_id = str(request.json.get('tg_id', '')).strip()
    perms = load_permissions()
    if target_tg_id in perms:
        del perms[target_tg_id]
        save_permissions(perms)
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
