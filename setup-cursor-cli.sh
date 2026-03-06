#!/bin/bash
#
# Cursor CLI Setup Script
# ========================
# 
# 用途：
#   为 Docker 容器环境准备 cursor-agent CLI 工具及其依赖
#   包括 cursor 脚本、index.js 和 Linux 版本的 Node.js 二进制文件
#
# 使用方法：
#   bash setup-cursor-cli.sh
#
# 前置要求：
#   - 已安装 cursor-agent（通过 'cursor agent' 命令安装）
#   - 系统需安装 curl 或 wget（用于下载 Node.js）
#   - 系统需安装 tar（用于解压 Node.js 归档文件）
#
# 输出：
#   在 docker/workspace/agents/ 目录下创建：
#   - cursor: cursor-agent 可执行脚本
#   - node: Linux x64 版本的 Node.js 二进制文件
#   - index.js: cursor-agent 的 JavaScript 入口文件
#

set -e  # 遇到错误立即退出

# ============================================================================
# 配置和路径设置
# ============================================================================

# 获取脚本所在目录（支持符号链接）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$SCRIPT_DIR/docker/workspace"
AGENTS_DIR="$WORKSPACE_DIR/agents"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 开始设置 Cursor CLI 环境"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ============================================================================
# 步骤 1: 查找 cursor-agent 安装位置
# ============================================================================

echo "📦 步骤 1/5: 查找 cursor-agent 安装..."
CURSOR_AGENT_BIN="$HOME/.local/bin/cursor-agent"

if [ ! -f "$CURSOR_AGENT_BIN" ]; then
    echo ""
    echo "❌ 错误: 未找到 cursor-agent"
    echo "   位置: $CURSOR_AGENT_BIN"
    echo ""
    echo "💡 解决方案:"
    echo "   请先安装 cursor-agent，运行以下命令："
    echo "   cursor agent"
    echo ""
    exit 1
fi

# 解析实际的 cursor-agent 路径（处理符号链接）
# macOS 可能不支持 readlink -f，所以需要兼容处理
CURSOR_AGENT_REAL=$(readlink -f "$CURSOR_AGENT_BIN" 2>/dev/null || \
                    readlink "$CURSOR_AGENT_BIN" 2>/dev/null || \
                    echo "$CURSOR_AGENT_BIN")
CURSOR_AGENT_DIR=$(dirname "$CURSOR_AGENT_REAL")

echo "   ✓ 找到 cursor-agent: $CURSOR_AGENT_DIR"
echo ""

# ============================================================================
# 步骤 2: 验证必需文件
# ============================================================================

echo "🔍 步骤 2/5: 验证必需文件..."

INDEX_JS="$CURSOR_AGENT_DIR/index.js"

if [ ! -f "$INDEX_JS" ]; then
    echo ""
    echo "❌ 错误: 缺少必需文件"
    echo "   文件: $INDEX_JS"
    echo ""
    echo "💡 这可能表示 cursor-agent 安装不完整，请重新安装："
    echo "   cursor agent"
    echo ""
    exit 1
fi

echo "   ✓ index.js 文件存在"
echo ""

# ============================================================================
# 步骤 3: 创建目录并复制 cursor CLI 文件
# ============================================================================

echo "📋 步骤 3/5: 复制 cursor CLI 文件..."

# 创建 agents 目录（如果不存在）
mkdir -p "$AGENTS_DIR"
echo "   ✓ 创建目录: $AGENTS_DIR"

# 复制 cursor 脚本（优先使用 cursor-agent 目录中的版本）
if [ -f "$CURSOR_AGENT_DIR/cursor-agent" ]; then
    cp "$CURSOR_AGENT_DIR/cursor-agent" "$AGENTS_DIR/cursor"
else
    cp "$CURSOR_AGENT_BIN" "$AGENTS_DIR/cursor"
fi

# 修复 cursor 脚本：移除 --use-system-ca 选项
# 原因：该选项仅在 Node.js 23.8.0+ 可用，且仅在 macOS/Windows 上支持
# 我们使用的 Node.js 22.11.0 在 Linux 上不支持此选项
echo "   ℹ️  正在修复 cursor 脚本以兼容 Linux Node.js..."
if grep -q "--use-system-ca" "$AGENTS_DIR/cursor"; then
    # 使用 sed 移除 --use-system-ca 选项
    # 匹配模式: "exec -a ... node --use-system-ca index.js"
    # 替换为: "exec -a ... node index.js"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS 使用 BSD sed（需要空字符串作为备份扩展名）
        sed -i '' 's/--use-system-ca //' "$AGENTS_DIR/cursor"
    else
        # Linux 使用 GNU sed
        sed -i 's/--use-system-ca //' "$AGENTS_DIR/cursor"
    fi
    
    # 验证修复是否成功
    if ! grep -q "--use-system-ca" "$AGENTS_DIR/cursor"; then
        echo "   ✓ 已移除不支持的 --use-system-ca 选项"
    else
        echo "   ⚠️  警告: 未能完全移除 --use-system-ca 选项，可能需要手动检查"
    fi
else
    echo "   ✓ cursor 脚本无需修改（未使用 --use-system-ca 选项）"
fi

echo "   ✓ 复制 cursor 脚本"

# 复制 index.js
cp "$INDEX_JS" "$AGENTS_DIR/index.js"
echo "   ✓ 复制 index.js"

# 复制所有原生绑定文件（.node 文件）
# 注意：这些文件是平台特定的
echo "   ℹ️  正在复制原生绑定文件..."
NODE_FILES_COUNT=0
for node_file in "$CURSOR_AGENT_DIR"/*.node; do
    if [ -f "$node_file" ]; then
        cp "$node_file" "$AGENTS_DIR/"
        NODE_FILES_COUNT=$((NODE_FILES_COUNT + 1))
    fi
done
if [ $NODE_FILES_COUNT -gt 0 ]; then
    echo "   ✓ 已复制 $NODE_FILES_COUNT 个原生绑定文件"
else
    echo "   ⚠️  警告: 未找到原生绑定文件"
fi

# 检查是否需要 Linux 版本的原生绑定文件
# index.js 在 Linux 上需要 merkle-tree-napi.linux-x64-gnu.node
LINUX_MERKLE="merkle-tree-napi.linux-x64-gnu.node"
LINUX_MERKLE_PATH="$AGENTS_DIR/$LINUX_MERKLE"
MACOS_MERKLE="merkle-tree-napi.darwin-x64.node"

if [ ! -f "$LINUX_MERKLE_PATH" ] && [ -f "$AGENTS_DIR/$MACOS_MERKLE" ]; then
    echo ""
    echo "   ⚠️  重要提示: 检测到 macOS 版本的原生绑定文件"
    echo "      Linux 容器需要: $LINUX_MERKLE"
    echo "      当前只有: $MACOS_MERKLE (macOS 版本)"
    echo ""
    echo "   💡 解决方案:"
    echo "      1. 在 Linux 系统上安装 cursor-agent，然后复制文件："
    echo "         - 在 Linux 系统运行: cursor agent"
    echo "         - 复制 ~/.local/share/cursor-agent/versions/*/merkle-tree-napi.linux-x64-gnu.node"
    echo "         - 放置到: $LINUX_MERKLE_PATH"
    echo ""
    echo "      2. 或者在 Docker 容器中安装 cursor-agent（agent.py 会自动处理）"
    echo ""
    echo "      3. 如果容器中已安装 cursor-agent，agent.py 会在运行时自动复制 Linux 版本的文件"
    echo ""
fi
echo ""

# ============================================================================
# 步骤 4: 下载并安装 Linux Node.js 二进制文件
# ============================================================================

echo "⬇️  步骤 4/5: 下载 Linux Node.js 二进制文件（用于 Docker 容器）..."

# Node.js 配置
# 注意: cursor 脚本要求 Node.js >= 22.1.0（支持编译缓存功能）
NODE_VERSION="22.11.0"  # 使用支持编译缓存的 LTS 版本
NODE_ARCH="x64"         # 架构：x64（适用于大多数 Docker 容器）
NODE_PLATFORM="linux"   # 平台：Linux（Docker 容器使用）

NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-${NODE_PLATFORM}-${NODE_ARCH}.tar.xz"
NODE_BIN="$AGENTS_DIR/node"

# 如果存在旧的 node 二进制文件，先删除
if [ -f "$NODE_BIN" ]; then
    echo "   ℹ️  检测到旧的 node 二进制文件，正在删除..."
    rm "$NODE_BIN"
fi

# 创建临时目录用于下载和提取
TEMP_DIR=$(mktemp -d)
# 设置退出时清理临时目录
trap "rm -rf $TEMP_DIR" EXIT

# 检查并选择下载工具
if command -v curl >/dev/null 2>&1; then
    DOWNLOAD_CMD="curl"
    DOWNLOAD_ARGS="-L -o"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD_CMD="wget"
    DOWNLOAD_ARGS="-O"
else
    echo ""
    echo "❌ 错误: 未找到下载工具"
    echo ""
    echo "💡 解决方案:"
    echo "   请安装 curl 或 wget 之一："
    echo "   - macOS: curl 通常已预装"
    echo "   - Linux: sudo apt-get install curl 或 sudo yum install wget"
    echo ""
    exit 1
fi

# 下载 Node.js
echo "   ℹ️  正在从 nodejs.org 下载 Node.js v${NODE_VERSION} (${NODE_PLATFORM}-${NODE_ARCH})..."
echo "   ℹ️  这可能需要几分钟，请耐心等待..."

if [ "$DOWNLOAD_CMD" = "curl" ]; then
    curl -L -o "$TEMP_DIR/node.tar.xz" "$NODE_URL"
else
    wget -O "$TEMP_DIR/node.tar.xz" "$NODE_URL"
fi

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ 错误: 下载 Node.js 失败"
    echo "   URL: $NODE_URL"
    echo ""
    echo "💡 可能的原因:"
    echo "   - 网络连接问题"
    echo "   - nodejs.org 服务器不可用"
    echo "   请检查网络连接后重试"
    echo ""
    exit 1
fi

echo "   ✓ 下载完成"

# 提取 Node.js 二进制文件
echo "   ℹ️  正在解压 Node.js 归档文件..."
tar -xf "$TEMP_DIR/node.tar.xz" -C "$TEMP_DIR" \
    "node-v${NODE_VERSION}-${NODE_PLATFORM}-${NODE_ARCH}/bin/node"

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ 错误: 解压 Node.js 归档文件失败"
    echo ""
    echo "💡 可能的原因:"
    echo "   - 归档文件损坏"
    echo "   - tar 命令不可用"
    echo "   请检查系统环境后重试"
    echo ""
    exit 1
fi

# 移动提取的二进制文件到目标目录
EXTRACTED_NODE="$TEMP_DIR/node-v${NODE_VERSION}-${NODE_PLATFORM}-${NODE_ARCH}/bin/node"

if [ ! -f "$EXTRACTED_NODE" ]; then
    echo ""
    echo "❌ 错误: 解压后未找到 node 二进制文件"
    echo "   预期位置: $EXTRACTED_NODE"
    echo ""
    echo "💡 这可能表示归档文件结构异常，请重试或联系支持"
    echo ""
    exit 1
fi

mv "$EXTRACTED_NODE" "$NODE_BIN"
echo "   ✓ 解压完成"

# 验证二进制文件格式（确保是 Linux ELF 格式）
echo "   ℹ️  正在验证二进制文件格式..."
if command -v file >/dev/null 2>&1; then
    if file "$NODE_BIN" | grep -q "ELF.*Linux"; then
        echo "   ✓ 验证通过: Node.js 二进制文件是 Linux ELF 格式"
    else
        echo ""
        echo "⚠️  警告: 二进制文件格式验证失败"
        echo "   文件类型: $(file "$NODE_BIN")"
        echo ""
        echo "💡 这可能导致 Docker 容器中无法执行"
        echo "   如果遇到问题，请检查 Docker 容器的架构是否匹配"
        echo ""
    fi
else
    echo "   ⚠️  跳过验证: 未找到 file 命令"
fi

echo ""

# ============================================================================
# 步骤 5: 设置文件权限
# ============================================================================

echo "🔐 步骤 5/5: 设置文件执行权限..."

chmod +x "$AGENTS_DIR/cursor"
echo "   ✓ cursor 脚本已设置为可执行"

chmod +x "$NODE_BIN"
echo "   ✓ node 二进制文件已设置为可执行"
echo ""

# ============================================================================
# 完成
# ============================================================================

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Cursor CLI 设置完成！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📁 已创建的文件:"
echo "   • cursor:    $AGENTS_DIR/cursor"
echo "   • node:      $NODE_BIN"
echo "                (Linux x64, v${NODE_VERSION})"
echo "   • index.js:  $AGENTS_DIR/index.js"
echo ""
echo "🎯 下一步:"
echo "   现在可以运行 bro 命令来使用 Docker 容器中的 cursor-agent："
echo "   bro run agent-backend --workspace docker/workspace"
echo ""
