"""
app.py - RH 代理服务（后厨）
职责：
  1. 接收 ComfyUI 节点的请求
  2. 调用 New API 验证 Key + 扣费
  3. 转发给 RunningHub（上传→提交→轮询）
  4. 返回结果给用户

部署：Railway（和 New API 同一个项目）
"""

import os
import time
import requests
from flask import Flask, request, jsonify, Response
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────
# 配置（通过 Railway 环境变量设置）
# ─────────────────────────────────────────────────────────────────
# New API 的地址（Railway 内网地址，同项目内直接访问）
NEW_API_URL = os.environ.get("NEW_API_URL", "http://new-api:3000")

# New API 的管理员 Token（在 New API 后台生成）
NEW_API_ADMIN_TOKEN = os.environ.get("NEW_API_ADMIN_TOKEN", "")

# RunningHub 的地址和你自己的 RH API Key
RH_BASE_URL = os.environ.get("RH_BASE_URL", "https://www.runninghub.cn")
RH_API_KEY  = os.environ.get("RH_API_KEY", "")  # 你自己的 RH Key

# 每次出图的费用（单位：New API 的额度单位，即美元）
# 例：0.1 表示每次出图扣 $0.10
COST_PER_IMAGE = float(os.environ.get("COST_PER_IMAGE", "0.1"))

# 轮询配置
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
POLL_TIMEOUT  = int(os.environ.get("POLL_TIMEOUT", "600"))

# ─────────────────────────────────────────────────────────────────
# New API 相关操作
# ─────────────────────────────────────────────────────────────────

def verify_key_and_get_balance(user_key: str) -> dict:
    """
    用用户的 Key 去 New API 查询：Key 是否有效、余额是否足够
    返回：{ "valid": bool, "balance": float, "user_id": int, "token_id": int }
    """
    try:
        resp = requests.get(
            f"{NEW_API_URL}/api/user/self",
            headers={"Authorization": f"Bearer {user_key}"},
            timeout=10,
        )
        if resp.status_code == 401:
            return {"valid": False, "reason": "Key 无效或已过期"}

        data = resp.json()
        if data.get("success") is False:
            return {"valid": False, "reason": data.get("message", "验证失败")}

        user_data = data.get("data", {})
        balance = user_data.get("quota", 0) / 500000  # New API 内部单位换算为美元

        return {
            "valid": True,
            "balance": balance,
            "user_id": user_data.get("id"),
            "username": user_data.get("username"),
        }
    except Exception as e:
        logger.error(f"验证 Key 失败: {e}")
        return {"valid": False, "reason": f"服务器错误: {str(e)}"}


def deduct_balance(user_key: str, cost: float, model_name: str, endpoint: str) -> bool:
    """
    通知 New API 扣费
    使用管理员 Token 操作，记录消费日志
    """
    try:
        # New API 内部额度单位 = 美元 × 500000
        quota_to_deduct = int(cost * 500000)

        resp = requests.post(
            f"{NEW_API_URL}/api/token/deduct",
            headers={
                "Authorization": f"Bearer {NEW_API_ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "key": user_key,
                "quota": quota_to_deduct,
                "model_name": model_name,
                "endpoint": endpoint,
            },
            timeout=10,
        )
        data = resp.json()
        return data.get("success", False)
    except Exception as e:
        logger.error(f"扣费失败: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# RunningHub 相关操作（和原来 nodes_execute.py 逻辑一致）
# ─────────────────────────────────────────────────────────────────

def rh_upload_image(image_data: bytes, filename: str) -> str:
    """上传图片到 RH，返回 download_url"""
    resp = requests.post(
        f"{RH_BASE_URL}/openapi/v2/media/upload/binary",
        headers={"Authorization": f"Bearer {RH_API_KEY}"},
        files={"file": (filename, image_data, "image/png")},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"RH 上传失败: {data.get('message')}")

    return data["data"]["download_url"]


def rh_submit_task(endpoint: str, body: dict) -> str:
    """提交生成任务，返回 taskId"""
    resp = requests.post(
        f"{RH_BASE_URL}/openapi/v2/{endpoint}",
        headers={
            "Authorization": f"Bearer {RH_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"RH 提交任务失败，无 taskId: {data}")

    return task_id


def rh_poll_result(task_id: str) -> list:
    """轮询任务结果，返回图片 URL 列表"""
    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        resp = requests.post(
            f"{RH_BASE_URL}/openapi/v2/query",
            headers={
                "Authorization": f"Bearer {RH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"taskId": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status", "")

        if status == "SUCCESS":
            results = data.get("results") or []
            if not results:
                raise RuntimeError("RH 任务成功但结果为空")
            return [r["url"] for r in results]

        if status in ("FAILED", "ERROR"):
            raise RuntimeError(f"RH 任务失败: {data.get('errorMessage', status)}")

        logger.info(f"任务状态: {status}，等待 {POLL_INTERVAL}s ...")
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"任务 {task_id} 超时（{POLL_TIMEOUT}s）")


# ─────────────────────────────────────────────────────────────────
# 接口路由
# ─────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify({"status": "ok", "service": "rh-proxy"})


@app.route("/openapi/v2/media/upload/binary", methods=["POST"])
def upload_image():
    """
    接收 ComfyUI 节点上传的图片
    → 验证 Key
    → 转发给 RH
    """
    # 从请求头获取用户 Key
    user_key = _extract_key(request)
    if not user_key:
        return jsonify({"code": 401, "message": "缺少 Authorization Header"}), 401

    # 验证 Key
    auth = verify_key_and_get_balance(user_key)
    if not auth["valid"]:
        return jsonify({"code": 401, "message": auth.get("reason", "Key 无效")}), 401

    # 检查余额
    if auth["balance"] < COST_PER_IMAGE:
        return jsonify({
            "code": 402,
            "message": f"余额不足（当前 ${auth['balance']:.2f}，需要 ${COST_PER_IMAGE:.2f}）"
        }), 402

    # 转发上传给 RH
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"code": 400, "message": "缺少 file 字段"}), 400

        download_url = rh_upload_image(file.read(), file.filename or "image.png")
        logger.info(f"用户 {auth['username']} 上传图片成功")

        # 返回和 RH 一样的格式，ComfyUI 节点不需要改代码
        return jsonify({
            "code": 0,
            "data": {"download_url": download_url}
        })

    except Exception as e:
        logger.error(f"上传失败: {e}")
        return jsonify({"code": 500, "message": str(e)}), 500


@app.route("/openapi/v2/<path:endpoint>", methods=["POST"])
def submit_task(endpoint):
    """
    接收 ComfyUI 节点的任务提交请求
    → 验证 Key
    → 扣费
    → 转发给 RH
    注意：/openapi/v2/query 也走这里，需要单独处理
    """
    # query 接口单独处理（不扣费，只查询）
    if endpoint == "query":
        return proxy_query()

    # 从请求头获取用户 Key
    user_key = _extract_key(request)
    if not user_key:
        return jsonify({"code": 401, "message": "缺少 Authorization Header"}), 401

    # 验证 Key 和余额
    auth = verify_key_and_get_balance(user_key)
    if not auth["valid"]:
        return jsonify({"code": 401, "message": auth.get("reason", "Key 无效")}), 401

    if auth["balance"] < COST_PER_IMAGE:
        return jsonify({
            "code": 402,
            "message": f"余额不足（当前 ${auth['balance']:.2f}，需要 ${COST_PER_IMAGE:.2f}）"
        }), 402

    # 提交任务给 RH
    try:
        body = request.get_json(force=True) or {}
        task_id = rh_submit_task(endpoint, body)

        # 扣费
        deducted = deduct_balance(user_key, COST_PER_IMAGE, endpoint, endpoint)
        if not deducted:
            logger.warning(f"扣费失败，但任务已提交: {task_id}")

        logger.info(f"用户 {auth['username']} 提交任务 {task_id}，扣费 ${COST_PER_IMAGE}")

        # 返回和 RH 一样的格式
        return jsonify({"taskId": task_id, "status": "RUNNING"})

    except Exception as e:
        logger.error(f"提交任务失败: {e}")
        return jsonify({"code": 500, "message": str(e)}), 500


def proxy_query():
    """
    轮询任务状态（不扣费，直接转发）
    验证 Key 有效即可
    """
    user_key = _extract_key(request)
    if not user_key:
        return jsonify({"code": 401, "message": "缺少 Authorization Header"}), 401

    auth = verify_key_and_get_balance(user_key)
    if not auth["valid"]:
        return jsonify({"code": 401, "message": auth.get("reason", "Key 无效")}), 401

    try:
        body    = request.get_json(force=True) or {}
        task_id = body.get("taskId")
        if not task_id:
            return jsonify({"code": 400, "message": "缺少 taskId"}), 400

        # 直接查一次，不等待（ComfyUI 节点自己会轮询）
        resp = requests.post(
            f"{RH_BASE_URL}/openapi/v2/query",
            headers={
                "Authorization": f"Bearer {RH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"taskId": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())

    except Exception as e:
        logger.error(f"查询任务失败: {e}")
        return jsonify({"code": 500, "message": str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# 管理接口（你自己用，不暴露给用户）
# ─────────────────────────────────────────────────────────────────

@app.route("/admin/balance/<user_key>", methods=["GET"])
def admin_check_balance(user_key):
    """查询某个 Key 的余额"""
    admin_token = request.headers.get("X-Admin-Token")
    if admin_token != NEW_API_ADMIN_TOKEN:
        return jsonify({"error": "无权限"}), 403

    auth = verify_key_and_get_balance(user_key)
    return jsonify(auth)


@app.route("/admin/config", methods=["GET"])
def admin_config():
    """查看当前配置（不含敏感信息）"""
    admin_token = request.headers.get("X-Admin-Token")
    if admin_token != NEW_API_ADMIN_TOKEN:
        return jsonify({"error": "无权限"}), 403

    return jsonify({
        "new_api_url": NEW_API_URL,
        "rh_base_url": RH_BASE_URL,
        "cost_per_image": COST_PER_IMAGE,
        "poll_interval": POLL_INTERVAL,
        "poll_timeout": POLL_TIMEOUT,
        "rh_key_configured": bool(RH_API_KEY),
        "admin_token_configured": bool(NEW_API_ADMIN_TOKEN),
    })


# ─────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────

def _extract_key(req) -> str:
    """从请求头提取 Bearer Token"""
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return ""


# ─────────────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
