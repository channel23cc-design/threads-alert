#!/usr/bin/env python3
"""
Threads @s_trader91 새 게시물 감지 → 카카오톡 알림
"""
import os
import json
import requests
import re
import sys
import asyncio
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


def get_threads_posts(username):
    """Playwright(실제 브라우저)로 Threads 게시물 가져오기"""
    try:
        return asyncio.run(_fetch_threads_posts(username))
    except Exception as e:
        print(f"게시물 가져오기 실패: {e}")
        return []


async def _fetch_threads_posts(username):
    from playwright.async_api import async_playwright

    captured_responses = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = await browser.new_page()

        # 네트워크 응답 가로채기 (GraphQL API 데이터 수집용)
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.body()
                    data = json.loads(body)
                    captured_responses.append({"url": response.url, "data": data})
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(
                f"https://www.threads.net/@{username}",
                wait_until="networkidle",
                timeout=30000,
            )
        except Exception as e:
            print(f"페이지 로드 오류 (계속 진행): {e}")

        # JS 렌더링 완료 대기
        await page.wait_for_timeout(3000)
        html = await page.content()
        await browser.close()

    print(f"캡처된 JSON 응답 수: {len(captured_responses)}")

    posts = []

    # 캡처된 API 응답에서 게시물 추출
    for resp in captured_responses:
        extracted = _find_posts_in_data(resp["data"], username)
        if extracted:
            print(f"응답에서 {len(extracted)}개 게시물 추출: {resp['url'][:80]}")
        posts.extend(extracted)

    # 실패 시 렌더링된 HTML에서 추출 시도
    if not posts:
        print("HTML 파싱으로 전환...")
        posts = _find_posts_in_html(html, username)

    # ID 기준 중복 제거
    seen_ids = set()
    unique_posts = []
    for post in posts:
        if post["id"] not in seen_ids:
            seen_ids.add(post["id"])
            unique_posts.append(post)

    return unique_posts


def _find_posts_in_data(data, username, _depth=0):
    """JSON 데이터를 재귀 탐색하여 thread_items 패턴 추출"""
    if _depth > 12 or not isinstance(data, (dict, list)):
        return []

    posts = []

    if isinstance(data, list):
        for item in data:
            posts.extend(_find_posts_in_data(item, username, _depth + 1))
        return posts

    # thread_items 키가 있으면 게시물 추출
    if "thread_items" in data:
        for item in data.get("thread_items") or []:
            if not isinstance(item, dict):
                continue
            post_data = item.get("post", {})
            pk = str(post_data.get("pk", "") or post_data.get("id", ""))
            caption = post_data.get("caption", {})
            text = caption.get("text", "") if isinstance(caption, dict) else ""
            code = post_data.get("code", "")
            taken_at = post_data.get("taken_at", 0)
            if pk and text:
                posts.append({
                    "id": pk,
                    "text": text,
                    "url": (
                        f"https://www.threads.net/@{username}/post/{code}"
                        if code
                        else f"https://www.threads.net/@{username}"
                    ),
                    "created_at": taken_at,
                })

    for val in data.values():
        if isinstance(val, (dict, list)):
            posts.extend(_find_posts_in_data(val, username, _depth + 1))

    return posts


def _find_posts_in_html(html, username):
    """렌더링된 HTML에서 thread_items JSON 블록 추출"""
    posts = []
    # thread_items 배열 패턴 탐색
    for match in re.findall(r'"thread_items"\s*:\s*(\[.{20,8000}?\])', html, re.DOTALL)[:10]:
        try:
            items = json.loads(match)
            for item in items:
                if not isinstance(item, dict):
                    continue
                post_data = item.get("post", {})
                pk = str(post_data.get("pk", "") or post_data.get("id", ""))
                caption = post_data.get("caption", {})
                text = caption.get("text", "") if isinstance(caption, dict) else ""
                code = post_data.get("code", "")
                taken_at = post_data.get("taken_at", 0)
                if pk and text:
                    posts.append({
                        "id": pk,
                        "text": text,
                        "url": (
                            f"https://www.threads.net/@{username}/post/{code}"
                            if code
                            else f"https://www.threads.net/@{username}"
                        ),
                        "created_at": taken_at,
                    })
        except Exception:
            pass
        if posts:
            break
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

    # 새 게시물만 필터링
    new_posts = []
    for post in posts:
        if post["id"] == last_seen_id:
            break
        new_posts.append(post)

    if not new_posts:
        print("새 게시물 없음")
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
