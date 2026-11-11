#!/bin/bash
# 让 smas 免密执行「关机/重启/电源管理」，以便 pi 会话可以自己重启本机。
# 用法（只需跑一次）：sudo bash scripts/mac/enable_nopasswd.sh
# 撤销：sudo rm /etc/sudoers.d/dsr-reboot
set -euo pipefail

SUDOERS_FILE=/etc/sudoers.d/dsr-reboot
USER_NAME="${SUDO_USER:-smas}"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root，请这样执行：sudo bash $0" >&2
  exit 1
fi

# 只放行固定的几条命令，不给全量 sudo
cat > "$SUDOERS_FILE" <<EOF
# 由 daily-stock-review/scripts/mac/enable_nopasswd.sh 生成
# 仅允许重启/关机与电源策略调整，其余命令仍需密码
${USER_NAME} ALL=(root) NOPASSWD: /sbin/shutdown -r now, /sbin/shutdown -h now, /usr/bin/pmset -a sleep 0, /usr/bin/pmset -a sleep 1, /usr/bin/pmset displaysleepnow
EOF

chown root:wheel "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"

if /usr/sbin/visudo -c -f "$SUDOERS_FILE" >/dev/null 2>&1; then
  echo "✅ 已写入 $SUDOERS_FILE"
  echo "   现在可以免密执行：sudo /sbin/shutdown -r now"
  echo "   验证：sudo -n -l | grep shutdown   （能看到条目即生效，不会真的重启）"
else
  rm -f "$SUDOERS_FILE"
  echo "❌ sudoers 语法检查失败，已回滚，未做任何改动" >&2
  exit 1
fi
