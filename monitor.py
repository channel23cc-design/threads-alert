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
THREADS_COOKIES = os.environ.get("THREADS_COOKIES", "")  # 로그인 쿠키 (선택)
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
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ko-KR",
        )

        # navigator.webdriver 속성 숨기기 (봇 감지 우회)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        # Threads 로그인 쿠키 주입
        if THREADS_COOKIES:
            try:
                raw_cookies = json.loads(THREADS_COOKIES)
                cookies = [
                    {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".threads.com"),
                        "path": c.get("path", "/"),
                    }
                    for c in raw_cookies
                    if c.get("name") and c.get("value")
                ]
                await context.add_cookies(cookies)
                print(f"로그인 쿠키 {len(cookies)}개 로드 완료")
            except Exception as e:
                print(f"쿠키 로드 실패: {e}")
        else:
            print("경고: THREADS_COOKIES 없음 — 비로그인 상태로 시도")

        page = await context.new_page()

        # GraphQL API 응답 가로채기
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.body()
                    if body and len(body) > 50:
                        try:
                            data = json.loads(body)
                            captured_responses.append({"url": response.url, "data": data})
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(
                f"https://www.threads.com/@{username}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            # JavaScript 실행 및 GraphQL 호출 완료 대기
            await page.wait_for_timeout(6000)
        except Exception as e:
            print(f"페이지 로드 오류 (계속): {e}")

        html = await page.content()
        await browser.close()

    print(f"캡처된 JSON 응답 수: {len(captured_responses)}")

    posts = []

    # GraphQL 응답에서 게시물 추출
    for resp in captured_responses:
        extracted = _find_posts_in_data(resp["data"], username)
        if extracted:
            print(f"응답에서 {len(extracted)}개 게시물 추출")
        posts.extend(extracted)

    # 실패 시 HTML에서 추출
    if not posts:
        print("HTML 파싱 시도...")
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
                        f"https://www.threads.com/@{username}/post/{code}"
                        if code
                        else f"https://www.threads.com/@{username}"
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
                            f"https://www.threads.com/@{username}/post/{code}"
                            if code
                            else f"https://www.threads.com/@{username}"
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

    # 첫 실행: 과거 게시물을 모두 보내지 않고 기준점만 저장
    if not last_seen_id:
        with open(LAST_SEEN_FILE, "w") as f:
            f.write(posts[0]["id"])
        print(f"첫 실행 완료 - 기준 ID 저장: {posts[0]['id']}")
        print("다음 실행부터 새 게시물 알림이 시작됩니다.")
        return

    # 새 게시물만 필터링 (마지막 확인 ID 이후의 것만)
    new_posts = []
    for post in posts:
        if post["id"] == last_seen_id:
            break
        new_posts.append(post)

    if not new_posts:
        print("새 게시물 없음")
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
