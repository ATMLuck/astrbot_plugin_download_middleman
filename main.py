import asyncio
import base64
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_download_middleman"

MAX_SEND_SIZE = 50 * 1024 * 1024  # 超过 50MB 的文件不再走 Onebot 发送
EXPIRE_DAYS = 3  # 下载目录中文件的保留天数

# GitHub 仓库 URL
GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE
)

# 净化文件名，防止路径穿越
_FILENAME_BAD = re.compile(r"[^\w.\-]")


@register(PLUGIN_NAME, "ATMLuck", "下载中间商：下载文件或克隆 GitHub 仓库并压缩发送", "1.0.0")
class DownloadMiddleman(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ---------- 路径 ----------

    @property
    def plugin_data_dir(self) -> Path:
        """插件数据目录 data/plugin_data/{plugin_name}/"""
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME

    def _get_download_dir(self) -> Path:
        """解析可配置下载目录，为空时回退到默认目录。"""
        cfg = (self.config.get("download_dir") or "").strip()
        d = Path(cfg) if cfg else self.plugin_data_dir / "DownloadFiles"
        if cfg and not d.is_absolute():
            d = self.plugin_data_dir / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 指令 ----------

    @filter.command("DW")
    async def dw(self, event: AstrMessageEvent, url: str = ""):
        '''下载文件或克隆 GitHub 仓库。'''
        url = (url or "").strip()
        if not url or not re.match(r"^https?://", url):
            logger.warning(f"网址无效: {url!r}")
            yield event.plain_result("网址无效")
            return

        logger.info(f"收到下载请求: {url}")

        await event.send(event.plain_result("开始处理，请稍候..."))

        try:
            # 清理下载目录中超过 3 天的旧文件
            await asyncio.to_thread(self._cleanup_old_files)

            if GITHUB_REPO_RE.match(url):
                logger.info(f"识别为 GitHub 仓库，开始克隆: {url}")
                await event.send(event.plain_result("识别为 GitHub 仓库，开始克隆并压缩为 zip..."))
                file_path = await self._clone_and_zip(url)
            else:
                if not await self._is_downloadable(url):
                    logger.warning(f"网址不可下载: {url}")
                    yield event.plain_result("网址无效")
                    return
                logger.info(f"开始下载文件: {url}")
                await event.send(event.plain_result("开始下载文件，请稍候..."))
                file_path = await self._download_file(url)

            if not file_path or not file_path.exists():
                logger.warning(f"下载失败: {url}")
                self._log(url, "", 0, False, "下载失败")
                yield event.plain_result("下载失败")
                return

            size = file_path.stat().st_size
            filename = file_path.name

            if size < MAX_SEND_SIZE:
                # 小于 50MB 直接通过 Onebot 发送文件
                logger.info(f"下载完成 {filename} ({size} B)，通过 Onebot 发送")
                await event.send(event.plain_result("下载完成，正在发送文件..."))
                yield await self._file_result(event, file_path)
                self._log(url, filename, size, True, "通过 Onebot 发送")
            else:
                # 大于等于 50MB 发送国内下载地址
                prefix = (self.config.get("download_url_prefix") or "").strip()
                if prefix:
                    logger.info(f"下载完成 {filename} ({size} B)，发送国内下载地址")
                    yield event.plain_result(f"国内下载地址：{prefix.rstrip('/')}/{filename}")
                    self._log(url, filename, size, True, "大文件，发送下载链接")
                else:
                    logger.info(f"下载完成 {filename} ({size} B)，未配置下载前缀，回退 Onebot 发送")
                    yield event.plain_result("无下载地址，请联系管理员配置！文件将通过Onebot发送！")
                    yield await self._file_result(event, file_path)
                    self._log(url, filename, size, True, "大文件，回退 Onebot 发送")
        except Exception as e:
            logger.error(f"下载异常: {url} - {e}")
            self._log(url, "", 0, False, str(e))
            yield event.plain_result(f"下载失败：{e}")

    # ---------- 下载 ----------

    async def _is_downloadable(self, url: str) -> bool:
        """探测 URL 是否可访问（2xx），优先 HEAD，失败回退 Range GET。"""
        if await self._run_curl_status(url):
            return True
        # HEAD 失败时回退 Range GET 探测
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sL", "-r", "0-0", "-o", "/dev/null", "-w", "%{http_code}",
            "--connect-timeout", "10", "--max-time", "30", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        code = out.decode(errors="replace").strip() if proc.returncode == 0 else None
        return bool(code and code.startswith("2"))

    async def _run_curl_status(self, url: str) -> str | None:
        """返回 HTTP 状态码；网络错误等返回 None。"""
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code}",
            "--connect-timeout", "10", "--max-time", "30", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return out.decode(errors="replace").strip() if proc.returncode == 0 else None

    async def _download_file(self, url: str) -> Path | None:
        """用 curl 将文件下载到下载目录，成功返回文件路径。"""
        download_dir = self._get_download_dir()
        dest = download_dir / self._safe_name(self._filename_from_url(url))
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}_{int(time.time())}{dest.suffix}")
        proc = await asyncio.create_subprocess_exec(
            "curl", "-L", "-f", "--connect-timeout", "15", "--max-time", "600",
            "-o", str(dest), url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"curl 下载失败: {stderr.decode(errors='replace')}")
            return None
        if not dest.exists() or dest.stat().st_size == 0:
            return None
        return dest

    async def _clone_and_zip(self, url: str) -> Path | None:
        """克隆 GitHub 仓库，压缩为 .zip，随后删除原文件夹。"""
        m = GITHUB_REPO_RE.match(url)
        repo_name = self._safe_name(m.group(2))
        download_dir = self._get_download_dir()
        tmp_dir = download_dir / f".tmp_clone_{int(time.time())}"

        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", url, str(tmp_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not tmp_dir.exists():
            logger.error(f"git clone 失败: {stderr.decode(errors='replace')}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        try:
            zip_path = download_dir / f"{repo_name}.zip"
            if zip_path.exists():
                zip_path = zip_path.with_name(f"{repo_name}_{int(time.time())}.zip")
            await asyncio.to_thread(self._zip_dir, tmp_dir, zip_path, repo_name)
            return zip_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _zip_dir(self, src_dir: Path, zip_path: Path, top_folder: str):
        """压缩目录（排除 .git），zip 内以 top_folder/ 为根目录。"""
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(src_dir):
                dirs[:] = [d for d in dirs if d != ".git"]
                for name in files:
                    full = os.path.join(root, name)
                    arc = os.path.join(top_folder, os.path.relpath(full, src_dir))
                    zf.write(full, arc)

    # ---------- 清理与日志 ----------

    def _cleanup_old_files(self):
        """删除下载目录中停留超过三天的文件/文件夹。"""
        download_dir = self._get_download_dir()
        if not download_dir.exists():
            return
        cutoff = time.time() - EXPIRE_DAYS * 24 * 3600
        for entry in download_dir.iterdir():
            try:
                if entry.stat().st_mtime < cutoff:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
            except OSError:
                continue

    def _log(self, url: str, filename: str, size: int, success: bool, note: str = ""):
        """记录下载日志到 data/plugin_data/{plugin_name}/Downloads.log。"""
        try:
            log_path = self.plugin_data_dir / "Downloads.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = "成功" if success else "失败"
            line = f"{now} | {url} | {filename} | {size} | {status}"
            if note:
                line += f" | {note}"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error(f"写入日志失败: {e}")

    # ---------- 工具 ----------

    @staticmethod
    def _safe_name(name: str) -> str:
        """净化文件/目录名，防止路径穿越。"""
        name = _FILENAME_BAD.sub("_", name).strip("._")
        return name or "download"

    @staticmethod
    def _filename_from_url(url: str) -> str:
        """从 URL 中提取文件名。"""
        path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        name = os.path.basename(path)
        return name or f"download_{int(time.time())}"

    async def _file_result(self, event: AstrMessageEvent, file_path: Path):
        """构造文件消息段；未配置 callback_api_base 时以 base64 内联发送。"""
        result = event.make_result()
        try:
            from astrbot.core import astrbot_config
            use_file_service = bool(astrbot_config.get("callback_api_base"))
        except Exception:
            use_file_service = False

        if use_file_service:
            logger.info(f"发送文件 {file_path.name} 走文件服务（callback_api_base）")
            result.chain = [Comp.File(name=file_path.name, file=str(file_path))]
            return result

        def _encode() -> str:
            with open(file_path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")

        logger.info(f"发送文件 {file_path.name} 走 base64 内联")
        bs64 = await asyncio.to_thread(_encode)
        result.chain = [Comp.File(name=file_path.name, url=f"base64://{bs64}")]
        return result
