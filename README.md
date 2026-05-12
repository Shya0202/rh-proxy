# RH 代理服务 - 部署说明

## 文件说明
- `app.py` - 主服务代码
- `requirements.txt` - Python 依赖
- `Procfile` - Railway 启动命令

## Railway 部署步骤

### 第一步：上传代码到 GitHub
1. 在 GitHub 新建一个仓库，比如叫 `rh-proxy`
2. 把这三个文件上传进去

### 第二步：在 Railway 添加新服务
1. 打开你的 Railway 项目
2. 点右上角 `+ Add`
3. 选 `GitHub Repo`
4. 选择你刚建的 `rh-proxy` 仓库
5. Railway 会自动识别 Python 项目并部署

### 第三步：配置环境变量
在 Railway 的 rh-proxy 服务里，点 `Variables`，添加以下变量：

| 变量名 | 值 | 说明 |
|--------|-----|------|
| `NEW_API_URL` | `http://new-api-frontend:3000` | New API 内网地址 |
| `NEW_API_ADMIN_TOKEN` | （你的管理员Token） | New API 后台生成 |
| `RH_BASE_URL` | `https://www.runninghub.cn` | RH 地址 |
| `RH_API_KEY` | （你的RH API Key） | 你自己的 RH 账号Key |
| `COST_PER_IMAGE` | `0.1` | 每次出图扣费金额（美元） |

### 第四步：获取 New API 管理员 Token
1. 登录 New API 后台
2. 左侧菜单 → 令牌管理 → 新建令牌
3. 名称填「管理员」，额度填 0（不限制），权限选最高
4. 复制生成的 sk-xxx Token
5. 填到 `NEW_API_ADMIN_TOKEN` 环境变量里

### 第五步：修改 ComfyUI 节点配置
把 `nodes_settings.py` 里的 base_url 默认值改成你的 Flask 服务地址：
```python
"base_url": ("STRING", {
    "default": "https://你的rh-proxy地址.railway.app",
    ...
}),
```

## 验证部署成功
访问 `https://你的地址.railway.app/health`
应该返回：`{"status": "ok", "service": "rh-proxy"}`

## 完整流程
```
用户 ComfyUI 节点
    ↓ 带 sk-xxx Key
rh-proxy（本服务）
    ↓ 验证Key + 扣费
New API
    ↓ 用你的RH Key转发
RunningHub
    ↓ 返回图片
用户
```
