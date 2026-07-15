from __future__ import annotations
import base64
import asyncio
import io
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import jmcomic
from groq import Groq
from flask import Flask
from waitress import serve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
SEARCH_LIMIT = 5
REQUEST_TIMEOUT = 15
if not TOKEN:
    raise RuntimeError("找不到 DISCORD_TOKEN 環境變數。")
if not CLOUDFLARE_ACCOUNT_ID:
    raise RuntimeError("找不到 CLOUDFLARE_ACCOUNT_ID 環境變數。")
if not CLOUDFLARE_API_TOKEN:
    raise RuntimeError("找不到 CLOUDFLARE_API_TOKEN 環境變數。")
    
@dataclass(slots=True, frozen=True)
class SearchResult:
    id: str
    title: str
    author: str | None
    publish_year: int | None = None
    cover_url: str | None = None
    detail_url: str | None = None
    
def limited_text(value: Any, limit: int, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"

def generate_ai_cover_prompt(
    title: str,
    author: str | None,
) -> str:
    author_text = ""
    if author and "未知" not in author:
        author_text = f", credited author: {author}"
    return (
        "Create a family-friendly fictional anime comic book cover, "
        "cinematic lighting, detailed illustration, professional layout, "
        "vertical cover composition, vibrant colors, "
        f"visual concept inspired by the title: {title}{author_text}. "
        "No nudity, no sexual content, no explicit content, "
        "no gore, no watermark, no words, no letters, no logo, clean composition."
    )

async def download_ai_cover(
    title: str,
    author: str | None,
) -> tuple[bytes | None, str | None, str | None]:
    api_url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )
    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": generate_ai_cover_prompt(title, author),
        "steps": 8,
        "seed": abs(hash(title)) % 2147483647,
    }
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                api_url,
                headers=headers,
                json=payload,
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    error_detail = (
                        data.get("errors", data)
                        if isinstance(data, dict)
                        else data
                    )
                    logging.error(
                        "Cloudflare AI 生成失敗：HTTP %s | %s",
                        response.status,
                        str(error_detail)[:500],
                    )
                    return (
                        None,
                        None,
                        f"Cloudflare HTTP {response.status}",
                    )
                if not isinstance(data, dict):
                    logging.error(
                        "Cloudflare AI 回傳格式錯誤：%r",
                        data,
                    )
                    return None, None, "圖片服務回傳格式錯誤"
                result = data.get("result")
                if not isinstance(result, dict):
                    logging.error(
                        "Cloudflare AI 缺少 result：%s",
                        str(data)[:500],
                    )
                    return None, None, "圖片結果不存在"
                image_base64 = result.get("image")
                if not isinstance(image_base64, str):
                    logging.error(
                        "Cloudflare AI 缺少 image：%s",
                        str(result)[:500],
                    )
                    return None, None, "圖片資料不存在"
                try:
                    image_bytes = base64.b64decode(
                        image_base64,
                        validate=True,
                    )
                except Exception:
                    logging.exception(
                        "Cloudflare AI 圖片 Base64 解碼失敗。"
                    )
                    return None, None, "圖片資料解碼失敗"
                if not image_bytes:
                    return None, None, "圖片內容為空"

                logging.info(
                    "Cloudflare AI 封面成功：%s bytes",
                    len(image_bytes),
                )
                return image_bytes, "jpg", None
    except asyncio.TimeoutError:
        logging.error("Cloudflare AI 圖片生成逾時。")
        return None, None, "圖片生成逾時"
    except aiohttp.ClientError as error:
        logging.error(
            "Cloudflare AI 連線失敗：%s",
            error,
        )
        return None, None, "圖片服務連線失敗"
    except Exception:
        logging.exception(
            "Cloudflare AI 封面處理發生未預期錯誤。"
        )
        return None, None, "圖片處理發生錯誤"

def get_discord_proxy_url(url: str | None) -> str | None:
    if not url:
        return None
    return f"https://discordapp.net{quote(url.replace('https://', '').replace('http://', ''))}"
def build_result_embed(
    result: SearchResult,
    keyword: str,
    index: int,
    total: int,
    ai_cover_url: str | None = None,
    image_error: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=limited_text(
            result.title,
            256,
            "未命名作品",
        ),
        url=result.detail_url,
        description=(
            "✨ **搜尋完成**\n"
            "使用下方按鈕切換其他搜尋結果。"
        ),
        color=discord.Color.from_rgb(
            88,
            101,
            242,
        ),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="👤 作者",
        value=limited_text(
            result.author,
            1024,
            "未知作者",
        ),
        inline=True,
    )
    embed.add_field(
        name="🆔 作品 ID",
        value=f"`{limited_text(result.id, 100, '未知')}`",
        inline=True,
    )
    embed.add_field(
        name="🔎 搜尋關鍵字",
        value=f"`{limited_text(keyword, 100, '未提供')}`",
        inline=False,
    )
    if image_error:
        embed.add_field(
            name="🖼️ 封面狀態",
            value=f"生成失敗：`{image_error}`",
            inline=False,
        )
    image_url = ai_cover_url or result.cover_url
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(
        text=(
            f"第 {index + 1} / {total} 筆"
            " • 按鈕將在 120 秒後停用"
        )
    )
    return embed

class SearchResultsView(discord.ui.View):
    def __init__(self, owner_id: int, keyword: str, results: list[SearchResult]) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.keyword = keyword
        self.results = results
        self.index = 0
        self.message = None
        self.ai_covers_cache: dict[int, tuple[bytes, str]] = {}
        self.update_buttons()

    async def current_page(
        self,
    ) -> tuple[discord.Embed, discord.File | None]:
        current_result = self.results[self.index]
        cached_cover = self.ai_covers_cache.get(self.index)
        image_error = None
        if cached_cover is None:
            image_bytes, extension, image_error = await download_ai_cover(
                current_result.title,
                current_result.author,
            )
            if image_bytes is not None and extension is not None:
                cached_cover = (
                    image_bytes,
                    extension,
                )
                self.ai_covers_cache[self.index] = cached_cover
        image_file = None
        image_url = None
        if cached_cover is not None:
            image_bytes, extension = cached_cover
            filename = f"cover_{self.index}.{extension}"
            image_file = discord.File(
                io.BytesIO(image_bytes),
                filename=filename,
            )
            image_url = f"attachment://{filename}"
        embed = build_result_embed(
            result=current_result,
            keyword=self.keyword,
            index=self.index,
            total=len(self.results),
            ai_cover_url=image_url,
            image_error=image_error,
        )
        return embed, image_file

    def update_buttons(self) -> None:
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.results) - 1

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "只有執行這個搜尋指令的人可以切換結果。",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
    @discord.ui.button(
        label="重新生成封面",
        emoji="🎨",
        style=discord.ButtonStyle.success,
    )
    async def regenerate_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.ai_covers_cache.pop(
            self.index,
            None,
        )
        await interaction.response.defer()
        embed, image_file = await self.current_page()
        await interaction.edit_original_response(
            embed=embed,
            attachments=[image_file] if image_file else [],
            view=self,
        )
    
    @discord.ui.button(
        label="上一頁",
        emoji="⬅️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.index <= 0:
            await interaction.response.defer()
            return
        self.index -= 1
        self.update_buttons()
        await interaction.response.defer()
        embed, image_file = await self.current_page()
        await interaction.edit_original_response(
            embed=embed,
            attachments=[image_file] if image_file else [],
            view=self,
        )

    @discord.ui.button(
        label="下一頁",
        emoji="➡️",
        style=discord.ButtonStyle.primary,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.index >= len(self.results) - 1:
            await interaction.response.defer()
            return
        self.index += 1
        self.update_buttons()
        await interaction.response.defer()
        embed, image_file = await self.current_page()
        await interaction.edit_original_response(
            embed=embed,
            attachments=[image_file] if image_file else [],
            view=self,
        )

_jm_client = None

def get_jm_client():
    global _jm_client
    if _jm_client is None:
        option = jmcomic.JmOption.default()
        _jm_client = option.new_jm_client()
    return _jm_client

class SearchBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def setup_hook(self) -> None:
        try:
            await asyncio.to_thread(get_jm_client)
            logging.info("核心 SDK 動態域名熱對接成功。")
        except Exception as e:
            logging.error(f"核心 SDK 初始化失敗: {e}")
        if TEST_GUILD_ID:
            try:
                guild_id = int(TEST_GUILD_ID)
            except ValueError as error:
                raise RuntimeError("TEST_GUILD_ID 必須是數字。") from error
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("已同步 %s 個指令到測試伺服器 %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            logging.info("已同步 %s 個全域指令", len(synced))

    async def search_content(self, keyword: str) -> list[SearchResult]:
        return await asyncio.to_thread(self._search_jmcomic_sync, keyword)

    def _search_jmcomic_sync(self, keyword: str) -> list[SearchResult]:
        try:
            client = get_jm_client()
            search_result = client.search_site(search_query=keyword, page=1)
            if not search_result:
                return []
            raw_content = search_result.content
            if isinstance(raw_content, tuple) and len(raw_content) == 2:
                target_list = raw_content[1] if isinstance(raw_content[1], list) else raw_content[0]
                if isinstance(raw_content[1], list):
                    raw_albums = raw_content[1][:SEARCH_LIMIT]
                elif isinstance(raw_content[0], list):
                    raw_albums = raw_content[0][:SEARCH_LIMIT]
                else:
                    raw_albums = [raw_content]
            elif isinstance(raw_content, list):
                raw_albums = raw_content[:SEARCH_LIMIT]
            else:
                raw_albums = [raw_content] if raw_content else []
            results: list[SearchResult] = []
            latest_domain = "jmcomic.me"
            if hasattr(client, 'api_client') and hasattr(client.api_client, 'domain'):
                latest_domain = client.api_client.domain
            elif hasattr(client, 'domain'):
                latest_domain = client.domain
            for album in raw_albums:
                if isinstance(album, tuple):
                    dict_item = next((item for item in album if isinstance(item, dict)), None)
                    if dict_item:
                        album_id = str(dict_item.get("id", ""))
                        title = str(dict_item.get("name") or dict_item.get("title") or "未命名作品")
                        author = str(dict_item.get("author") or "未知作者")
                    else:
                        album_id = str(album[0]) if len(album) > 0 else ""
                        title = str(album[1]) if len(album) > 1 else "未命名作品"
                        author = str(album[2]) if len(album) > 2 else "未知作者"
                else:
                    album_id = str(getattr(album, "id", "") or getattr(album, "album_id", "") or (album.get("id") if isinstance(album, dict) else ""))
                    title = getattr(album, "name", None) or getattr(album, "title", None) or (album.get("title") if isinstance(album, dict) else "未命名作品")
                    author = getattr(album, "author", None) or getattr(album, "artist", None) or "未知作者"
                if isinstance(author, list):
                    author = "、".join(str(a) for a in author)
                detail_url = f"https://{latest_domain}/album/{album_id}" if album_id else None
                results.append(
                    SearchResult(
                        id=str(album_id),
                        title=str(title),
                        author=str(author),
                        cover_url=None,
                        detail_url=detail_url
                    )
                )
            return results
        except Exception as e:
            logging.error(f"底層套件解包查詢時發生異常: {e}", exc_info=True)
            return []

web_app = Flask(__name__)

@web_app.get("/")
def home():
    return {
        "status": "online",
        "service": "discord-bot"
    }, 200

@web_app.get("/health")
def health():
    return {
        "status": "healthy"
    }, 200

def run_web_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    serve(
        web_app,
        host="0.0.0.0",
        port=port,
    )

bot = SearchBot()

@bot.event
async def on_ready() -> None:
    if not bot.user:
        return
    logging.info("機器人已上線：%s | ID：%s", bot.user, bot.user.id)

@bot.tree.command(
    name="search",
    description="使用關鍵字搜尋作品",
)

@app_commands.describe(
    keyword="請輸入作品名稱或搜尋關鍵字",
)

@app_commands.checks.cooldown(
    1,
    5,
    key=lambda interaction: interaction.user.id,
)
async def search_command(
    interaction: discord.Interaction,
    keyword: str,
) -> None:
    keyword = keyword.strip()
    if not keyword:
        await interaction.response.send_message(
            "請輸入有效的搜尋關鍵字。",
            ephemeral=True,
        )
        return

    if len(keyword) > 100:
        await interaction.response.send_message(
            "搜尋關鍵字不能超過 100 個字元。",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        results = await bot.search_content(keyword)
    except asyncio.TimeoutError:
        await interaction.followup.send(
            "搜尋逾時，請稍後重新嘗試。",
            ephemeral=True,
        )
        return
    except Exception:
        logging.exception(
            "搜尋時發生未預期錯誤，keyword=%r",
            keyword,
        )
        await interaction.followup.send(
            "搜尋時發生未預期錯誤。",
            ephemeral=True,
        )
        return
    if not results:
        await interaction.followup.send(
            f"找不到與「{keyword}」相關的結果。",
            ephemeral=True,
        )
        return
    view = SearchResultsView(
        owner_id=interaction.user.id,
        keyword=keyword,
        results=results,
    )
    first_embed, first_file = await view.current_page()
    view.update_buttons()
    send_options = {
        "embed": first_embed,
        "view": view,
        "wait": True,
    }
    if first_file:
        send_options["file"] = first_file
    message = await interaction.followup.send(
        **send_options
    )
    view.message = message

@search_command.error
async def search_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        message = (
            f"操作速度太快，請在 "
            f"{error.retry_after:.1f} 秒後重新嘗試。"
        )
    else:
        logging.error(
            "斜線指令發生錯誤。",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )
        message = "執行指令時發生錯誤。"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

web_thread = threading.Thread(
    target=run_web_server,
    daemon=True,
)
web_thread.start()

bot.run(TOKEN)
