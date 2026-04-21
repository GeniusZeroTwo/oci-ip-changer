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

        # 1. 获取网卡 (VNIC) 信息
        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=OCI_CONFIG["tenancy"], instance_id=target_ocid).data
        if not vnic_attachments:
            raise Exception("未找到该实例的网卡 (VNIC)")
        vnic_id = vnic_attachments[0].vnic_id
        
        # 2. 获取内网 IP (Private IP) 信息
        private_ips = vnc_client.list_private_ips(vnic_id=vnic_id).data
        if not private_ips:
            raise Exception("未找到该实例的内网 IP")
        primary_private_ip_id = private_ips[0].id

        old_ip = "Unknown"

        # 3. 【核心升级】智能检测并清理当前绑定的公网 IP (兼容临时与保留IP)
        try:
            get_ip_details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=primary_private_ip_id)
            public_ip = vnc_client.get_public_ip_by_private_ip_id(get_ip_details).data
            old_ip = public_ip.ip_address
            
            if public_ip.lifetime == 'RESERVED':
                # 如果发现是保留 IP，为了安全起见不删除它，而是通过传入空字符串来"解绑"
                update_details = oci.core.models.UpdatePublicIpDetails(private_ip_id="")
                vnc_client.update_public_ip(public_ip.id, update_details)
            else:
                # 如果是临时 IP，直接彻底删除
                vnc_client.delete_public_ip(public_ip.id)
                
            # 缓冲时间：给甲骨文云端2秒钟释放网络资源，彻底避免 409 冲突
            time.sleep(2)
        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                old_ip = "None" # 当前网卡上干干净净，没有绑定任何公网 IP
            else:
                raise e

        # 4. 创建并绑定全新的临时公网 IP
        create_details = oci.core.models.CreatePublicIpDetails(
            compartment_id=OCI_CONFIG["tenancy"], 
            lifetime="EPHEMERAL", 
            private_ip_id=primary_private_ip_id
        )
        new_ip = vnc_client.create_public_ip(create_details).data.ip_address
        
        return old_ip, new_ip
    except Exception as e:
        error_msg = str(e)
        print(f"OCI API 错误: {error_msg}")
        send_tg_message(ADMIN_ID, f"⚠️ **甲骨文 API 报错详情**\n\n
