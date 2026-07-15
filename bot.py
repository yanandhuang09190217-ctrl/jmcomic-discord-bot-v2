from __future__ import annotations
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
import discord
from discord import app_commands
from discord.ext import commands
import jmcomic
from groq import Groq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
POLLINATIONS_KEY = os.getenv("POLLINATIONS_KEY")
SEARCH_LIMIT = 5
REQUEST_TIMEOUT = 15
if not TOKEN:
    raise RuntimeError("找不到 DISCORD_TOKEN 環境變數。")
if not POLLINATIONS_KEY:
    raise RuntimeError("找不到 POLLINATIONS_KEY 環境變數。")

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

def generate_ai_cover_url(title: str, author: str | None) -> str:
    author_text = ""
    if author and "未知" not in author:
        author_text = f", author name: {author}"
    prompt = (
        "A safe anime-style fictional comic book cover, "
        "vibrant colors, highly detailed illustration, "
        f"book title: {title}{author_text}, "
        "no nudity, no sexual content, no explicit content"
    )
    encoded_prompt = quote(prompt, safe="")
    parameters = urlencode(
        {
            "width": 768,
            "height": 1024,
            "model": "flux",
            "nologo": "true",
            "seed": -1,
            "key": POLLINATIONS_KEY,
        }
    )
    return (
        f"https://gen.pollinations.ai/image/"
        f"{encoded_prompt}?{parameters}"
    )

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
) -> discord.Embed:
    description_parts = []
    if result.author:
        description_parts.append(f"**作者：** {result.author}")
    description_parts.append(f"**作品 ID：** {result.id}")
    description_parts.append(f"**搜尋關鍵字：** {keyword}")
    description_parts.append("\n💡 *點擊上方藍色標題可前往網頁*")
    embed = discord.Embed(
        title=limited_text(result.title, 256, "未命名作品"),
        url=result.detail_url,
        description="\n".join(description_parts),
        color=discord.Color.blurple(),
    )
    image_url = ai_cover_url or result.cover_url
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(
        text=f"搜尋結果 {index + 1}/{total}"
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
        self.ai_covers_cache: dict[int, str] = {}
        self.update_buttons()

    async def current_embed(self) -> discord.Embed:
        current_result = self.results[self.index]
        cover_url = self.ai_covers_cache.get(self.index)
        if cover_url is None:
            cover_url = generate_ai_cover_url(
                current_result.title,
                current_result.author,
            )
            self.ai_covers_cache[self.index] = cover_url
        logging.info("AI 圖片網址已成功產生，結果頁數：%s", self.index + 1)
        return build_result_embed(
            result=current_result,
            keyword=self.keyword,
            index=self.index,
            total=len(self.results),
            ai_cover_url=cover_url,
        )

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
        await interaction.response.edit_message(
            embed=await self.current_embed(),
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
        await interaction.response.edit_message(
            embed=await self.current_embed(),
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

    first_embed = await view.current_embed()
    view.update_buttons()

    message = await interaction.followup.send(
        embed=first_embed,
        view=view,
        wait=True,
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

bot.run(TOKEN)