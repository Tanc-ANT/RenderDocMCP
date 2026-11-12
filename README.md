# RenderDoc MCP 服务器

通过 RenderDoc 界面扩展提供的 MCP 服务器，让 AI 助手能够访问 RenderDoc 截帧数据，辅助图形调试。

## 架构

```
Claude / AI 客户端（stdio）
        │
        ▼
MCP 服务器进程（Python + FastMCP 2.0）
        │ 文件 IPC（%TEMP%/renderdoc_mcp/）
        ▼
RenderDoc 进程（扩展）
```

由于 RenderDoc 内置的 Python 不提供 socket 模块，因此使用文件 IPC 进行进程间通信。

## 安装与配置

### 1. 安装 RenderDoc 扩展

```powershell
python scripts/install_extension.py
```

扩展将安装到 `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge`。

### 2. 在 RenderDoc 中启用扩展

1. 启动 RenderDoc。
2. 打开 Tools > Manage Extensions。
3. 启用 "RenderDoc MCP Bridge"。

### 3. 安装 MCP 服务器

```powershell
uv tool install .
uv tool update-shell  # 添加到 PATH
```

重新打开终端后，即可使用 `renderdoc-mcp` 命令。

> **提示**：开发时可使用 `uv tool install --editable .`，让源码修改直接生效。
> 常规安装使用 `uv tool install .`。

### 4. 配置 MCP 客户端

#### Claude Desktop

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

#### Claude Code

在 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

## 使用方法

1. 启动 RenderDoc 并打开截帧文件（.rdc）。
2. 通过 MCP 客户端（如 Claude）访问 RenderDoc 数据。

## MCP 工具列表

| 工具 | 说明 |
|--------|------|
| `get_capture_status` | 检查截帧加载状态 |
| `get_draw_calls` | 获取具有层级结构的绘制调用列表 |
| `get_draw_call_details` | 获取指定绘制调用的详细信息 |
| `get_shader_info` | 获取着色器源码和常量缓冲区的值 |
| `get_buffer_contents` | 获取缓冲区内容（Base64） |
| `get_texture_info` | 获取纹理元数据 |
| `get_texture_data` | 获取纹理像素数据（Base64） |
| `get_pipeline_state` | 获取管线状态 |

## 使用示例

### 获取绘制调用列表

```
get_draw_calls(include_children=true)
```

### 获取着色器信息

```
get_shader_info(event_id=123, stage="pixel")
```

### 获取管线状态

```
get_pipeline_state(event_id=123)
```

### 获取纹理数据

```
# 获取 2D 纹理的 mip 0
get_texture_data(resource_id="ResourceId::123")

# 获取指定 mip 级别
get_texture_data(resource_id="ResourceId::123", mip=2)

# 获取立方体贴图的指定面（0=X+、1=X-、2=Y+、3=Y-、4=Z+、5=Z-）
get_texture_data(resource_id="ResourceId::456", slice=3)

# 获取 3D 纹理的指定深度切片
get_texture_data(resource_id="ResourceId::789", depth_slice=5)
```

### 读取部分缓冲区数据

```
# 读取整个缓冲区
get_buffer_contents(resource_id="ResourceId::123")

# 从偏移量 256 开始读取 512 字节
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

## 环境要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+

> **提示**：原项目仅在 Windows + DirectX 11 环境中验证过。
> Linux/macOS + Vulkan/OpenGL 环境可能也可运行，但尚未验证。

## 许可证

MIT
