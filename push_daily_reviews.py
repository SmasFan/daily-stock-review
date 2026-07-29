"""
生成复盘页面并推送到 GitHub 私有仓库
用法：
  python push_daily_reviews.py --market a-share --type post
  python push_daily_reviews.py --market us --type pre
  python push_daily_reviews.py --market us --type post
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(BASE_DIR, 'daily-stock-review')

# 把 git 和 gh 加入 PATH
os.environ['PATH'] = os.environ.get('PATH', '') + r';C:\Program Files\Git\bin;C:\Program Files\GitHub CLI'


def run(cmd, cwd=None, check=True):
    print(f'> {" ".join(cmd)}')
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f'命令失败: {" ".join(cmd)}')
    return result


def main():
    parser = argparse.ArgumentParser(description='生成复盘页面并推送到 GitHub')
    parser.add_argument('--market', choices=['a-share', 'us'], required=True)
    parser.add_argument('--type', choices=['pre', 'post'], required=True)
    args = parser.parse_args()

    # 1. 生成复盘页面
    run([sys.executable, 'post_market_review.py', '--market', args.market, '--type', args.type], cwd=BASE_DIR)

    # 2. 复制到仓库目录
    html_file = 'review_a_share.html' if args.market == 'a-share' else f'review_us_{args.type}.html'
    src = os.path.join(BASE_DIR, html_file)
    dst = os.path.join(REPO_DIR, html_file)
    shutil.copy2(src, dst)
    print(f'已复制 {html_file} 到仓库目录')

    # 3. 配置本地 git 身份（不修改全局）
    run(['git', 'config', 'user.email', 'SmasFan@users.noreply.github.com'], cwd=REPO_DIR, check=False)
    run(['git', 'config', 'user.name', 'SmasFan'], cwd=REPO_DIR, check=False)

    # 4. 提交并推送
    run(['git', 'add', '.'], cwd=REPO_DIR)

    status = run(['git', 'status', '--porcelain'], cwd=REPO_DIR, check=False)
    if status.stdout.strip():
        msg = f"update {args.market} {args.type} review {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        run(['git', 'commit', '-m', msg], cwd=REPO_DIR)

        # 推送重试，网络偶发失败时可自动恢复
        last_error = None
        for attempt in range(3):
            try:
                run(['git', 'push', 'origin', 'main'], cwd=REPO_DIR)
                print(f'已推送到 GitHub: {msg}')
                break
            except RuntimeError as e:
                last_error = e
                print(f'推送失败，第 {attempt + 1} 次重试...')
        else:
            raise last_error
    else:
        print('没有变更，跳过提交')


if __name__ == '__main__':
    main()
