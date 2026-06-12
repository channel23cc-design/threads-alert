#!/usr/bin/env python3
"""
Threads @s_trader91 새 게시물 감지 → 카카오톡 알림
"""
import os
import json
import requests
import re
import sys
from datetime import datetime

THREADS_USERNAME = "s_trader91"
KAKAO_REST_API_KEY = os.environ["KAKAO_REST_API_KEY"]
KAKAO_REFRESH_TOKEN = os.environ["KAKAO_REFRESH_TOKEN"]
KAKAO_CLIENT_SECRET = os.environ["KAKAO_CLIENT_SECRET"]
LAST_SEEN_FILE = "last_seen_id.txt"


def get_kakao_access_token():
    """refresh_token 으로 새 access_token 발급"""
    response = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": KAKAO_REST_API_KEY,
            "refresh_token": KAKAO_REFRESH_TOKEN,
            "client_secret": KAKAO_CLIENT_SECRET,
        },
    )
    result = response.json()
    if "access_token" not in result:
        print(f"토큰 발급 실패: {result}")
        sys.exit(1)
    return result["access_token"]


def send_kakao_message(access_token, text, url):
    """카카오톡 나에게 메시지 보내기"""
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": url, "mobile_web_url": url},
    }
    response = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
    )
    return response.json()


def get_threads_user_id(username):
    """Threads 유저 이름 → 숫자 ID 변환"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    r = requests.get(f"https://www.threads.net/@{username}", headers=headers, timeout=15)
    # 페이지 HTML에서 user_id 추출
    match = re.search(r'"user_id"\s*:\s*"(\d+)"', r.text)
    if match:
        return match.group(1)
    # 두 번째 패턴 시도
    match = re.search(r'"pk"\s*:\s*"(\d+)"', r.text)
    if match:
        return match.group(1)
    return None


def get_threads_posts(username):
    """최근 게시물 목록 반환 (각 항목: id, text, url, created_at)"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Cache-Control": "no-cache",
    }

    try:
        r = requests.get(
            f"https://www.threads.net/@{username}", headers=headers, timeout=20
        )
    except Exception as e:
        print(f"Threads 요청 실패: {e}")
        return []

    # 페이지 HTML 안의 JSON 데이터 파싱
    # Next.js __NEXT_DATA__ 또는 embedded JSON 추출
    posts = []

    # 방법 1: __SSR_DATA__ 패턴
    match = re.search(r'__SSR_DATA__\s*=\s*({.+?});\s*</script>', r.text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            posts = _extract_posts_from_data(data, username)
        except Exception:
            pass

    # 방법 2: require("RelayPrefetchedStreamCache") 패턴
    if not posts:
        matches = re.findall(r'\["RelayPrefetchedStreamCache"[^\]]*\]', r.text)
        for m in matches:
            try:
                data = json.loads(m)
                posts = _extract_posts_from_relay(data, username)
                if posts:
                    break
            except Exception:
                pass

    # 방법 3: 페이지 내 JSON 블록 전체 탐색
    if not posts:
        json_blocks = re.findall(r'\{[^{}]*"thread_items"[^{}]*\}', r.text)
        for block in json_blocks[:5]:
            try:
                data = json.loads(block)
                extracted = _extract_from_thread_items(data.get("thread_items", []), username)
                posts.extend(extracted)
            except Exception:
                pass

    return posts


def _extract_posts_from_data(data, username):
    """SSR 데이터에서 게시물 추출"""
    posts = []
    try:
        edges = (
            data.get("data", {})
            .get("userData", {})
            .get("user", {})
            .get("threads", {})
            .get("edges", [])
        )
        for edge in edges:
            items = edge.get("node", {}).get("thread_items", [])
            posts.extend(_extract_from_thread_items(items, username))
    except Exception:
        pass
    return posts


def _extract_posts_from_relay(data, username):
    posts = []
    text = json.dumps(data)
    items_matches = re.findall(r'"thread_items"\s*:\s*(\[[^\]]+\])', text)
    for match in items_matches:
        try:
            items = json.loads(match)
            posts.extend(_extract_from_thread_items(items, username))
        except Exception:
            pass
    return posts


def _extract_from_thread_items(items, username):
    posts = []
    for item in items:
        try:
            post = item.get("post", {})
            pk = str(post.get("pk", "") or post.get("id", ""))
            caption = post.get("caption", {})
            text = caption.get("text", "") if isinstance(caption, dict) else ""
            code = post.get("code", "")
            taken_at = post.get("taken_at", 0)
            if pk and text:
                posts.append({
                    "id": pk,
                    "text": text,
                    "url": f"https://www.threads.net/@{username}/post/{code}" if code else f"https://www.threads.net/@{username}",
                    "created_at": taken_at,
                })
        except Exception:
            continue
    return posts


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 모니터링 시작")

    # 마지막으로 확인한 게시물 ID 로드
    last_seen_id = None
    if os.path.exists(LAST_SEEN_FILE):
        with open(LAST_SEEN_FILE, "r") as f:
            last_seen_id = f.read().strip()
    print(f"마지막 확인 ID: {last_seen_id or '없음'}")

    # Threads 게시물 가져오기
    posts = get_threads_posts(THREADS_USERNAME)
    print(f"가져온 게시물 수: {len(posts)}")

    if not posts:
        print("게시물을 가져오지 못했습니다. 종료.")
        return

    # 새 게시물만 필터링 (ID가 last_seen_id 이전까지)
    new_posts = []
    for post in posts:
        if post["id"] == last_seen_id:
            break
        new_posts.append(post)

    if not new_posts:
        print("새 게시물 없음")
        # 최신 ID 저장 (첫 실행 시)
        if not last_seen_id:
            with open(LAST_SEEN_FILE, "w") as f:
                f.write(posts[0]["id"])
            print(f"첫 실행 - 기준 ID 저장: {posts[0]['id']}")
        return

    print(f"새 게시물 {len(new_posts)}개 발견!")

    # 카카오톡 알림 전송
    access_token = get_kakao_access_token()

    for post in reversed(new_posts):  # 오래된 것부터 전송
        preview = post["text"][:80] + ("..." if len(post["text"]) > 80 else "")
        message = f"📢 @{THREADS_USERNAME} 새 게시물\n\n{preview}\n\n🔗 게시물 보기"
        result = send_kakao_message(access_token, message, post["url"])
        print(f"메시지 전송 결과: {result}")

    # 가장 최신 ID 저장
    with open(LAST_SEEN_FILE, "w") as f:
        f.write(posts[0]["id"])
    print(f"최신 ID 저장: {posts[0]['id']}")
    print("완료!")


if __name__ == "__main__":
    main()
