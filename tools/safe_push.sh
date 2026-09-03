#!/usr/bin/env bash
# safe_push.sh — 统一的安全推送脚本
#
# 目的：解决 9 个 GitHub Actions workflow 并发 `git push` 直写 main 导致的
#       "本地领先 N 提交 / non-fast-forward" 推送冲突。
#
# 策略（最小侵入，不改业务逻辑）：
#   1. 先 `git pull --rebase` 拉取远端最新，把自己即将提交的改动 rebase 到最新之上；
#   2. `git push`，若因并发导致 non-fast-forward，则自动重试（最多 3 次）；
#   3. 每次重试之间做小幅随机退避，避免多个 runner 同时重试再次撞车。
#
# 用法（在 workflow 的 step 中调用）：
#   bash tools/safe_push.sh "chore: update xxx snapshot"
#   或者（如果前面已经单独 commit 过，只想安全 push）：
#   bash tools/safe_push.sh --push-only
#
# 注意：调用前必须先 `git config user.name/email` 设置好提交者身份。

set -euo pipefail

MAX_RETRY="${SAFE_PUSH_MAX_RETRY:-3}"
RETRY_BACKOFF="${SAFE_PUSH_RETRY_BACKOFF:-4}"

# 提交信息（可选参数）
COMMIT_MSG="${1:-}"

# 配置身份（若未设置则用默认 bot 身份，避免遗漏）
if ! git config user.name >/dev/null 2>&1; then
  git config user.name "github-actions[bot]"
fi
if ! git config user.email >/dev/null 2>&1; then
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
fi

# 如果没有待提交改动，直接尝试同步远端（保证本地干净后 push 不会失败）
if [ -z "$(git status --porcelain)" ]; then
  echo "[safe_push] 工作区干净，无需提交，直接推送。"
  # 空转：无改动也无 commit 需求，直接拉取对齐并退出
  git pull --rebase --autostash origin "${GITHUB_REF_NAME:-main}" >/dev/null 2>&1 || true
  exit 0
fi

# 若调用方未单独 commit（传了提交信息），则统一在此提交
if [ -n "${COMMIT_MSG}" ] && [ "${COMMIT_MSG}" != "--push-only" ]; then
  git diff --cached --quiet || git commit -m "${COMMIT_MSG}"
  # 若有未暂存改动，一并 add 后提交（兼容调用方先 add 或未 add 两种写法）
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git diff --cached --quiet || git commit -m "${COMMIT_MSG}"
  fi
fi

# 带重试的 push（pull --rebase 前置，解决 non-fast-forward）
attempt=1
while [ "$attempt" -le "$MAX_RETRY" ]; do
  echo "[safe_push] 第 ${attempt}/${MAX_RETRY} 次尝试：pull --rebase ..."
  if git pull --rebase --autostash origin "${GITHUB_REF_NAME:-main}"; then
    echo "[safe_push] 第 ${attempt}/${MAX_RETRY} 次尝试：git push ..."
    if git push origin "${GITHUB_REF_NAME:-main}"; then
      echo "[safe_push] 推送成功。"
      exit 0
    fi
  fi

  # rebase 冲突：放弃本次改动，恢复到安全状态
  if git ls-files -u | grep -q .; then
    echo "[safe_push] 检测到 rebase 冲突，中止并还原，保留工作区改动。"
    git rebase --abort >/dev/null 2>&1 || true
    git reset --hard HEAD >/dev/null 2>&1 || true
  fi

  if [ "$attempt" -lt "$MAX_RETRY" ]; then
    sleep $(( RETRY_BACKOFF * attempt ))
  fi
  attempt=$(( attempt + 1 ))
done

echo "[safe_push] 重试 ${MAX_RETRY} 次仍失败，放弃推送（改动保留在工作区，下次调度会重试）。"
exit 1
