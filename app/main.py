from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os
import shutil

from .database import create_tables, get_db
from .models import Config
from .paths import get_nas_root
from .routers import config_router, files_router, youtube_router, audio_router, sync_router
from .download_manager import download_manager
from .compressor import compressor
from .nas_mount import mount_nas

app = FastAPI(title="NAS File Browser + YouTube Audio Downloader")

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates
templates = Jinja2Templates(directory="app/templates")

# Add custom filters
def filesizeformat(value):
    """Format file size in human readable format"""
    if value == 0:
        return "0 B"
    
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    size = float(value)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    
    return f"{size:.1f} {units[i]}"

templates.env.filters['filesizeformat'] = filesizeformat

def path_to_id(path):
    """Encode a file path into a CSS-safe ID segment (no /, no special chars)."""
    from urllib.parse import quote
    return quote(path, safe='').replace('%2F', '--')

templates.env.filters['path_to_id'] = path_to_id

# Create database tables on startup
@app.on_event("startup")
async def startup_event():
    create_tables()

    # Migration: add 'tag' column to file_metadata if missing
    try:
        from .database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(file_metadata)")).fetchall()]
            if "tag" not in cols:
                conn.execute(text("ALTER TABLE file_metadata ADD COLUMN tag VARCHAR(50)"))
                conn.commit()
                print("[DB] Added 'tag' column to file_metadata")
    except Exception as e:
        print(f"[DB] Migration note: {e}")

    # Start download manager
    await download_manager.start()

    # Set compressor event loop for thread-safe WebSocket broadcasts
    import asyncio
    compressor.set_loop(asyncio.get_running_loop())
    
    # Insert default config if not exists
    db = next(get_db())
    try:
        # Check if NAS root config exists
        nas_root = db.query(Config).filter(Config.key == "nas_root").first()
        if not nas_root:
            db.add(Config(key="nas_root", value="/nas", description="Root directory for NAS files"))
        
        # Check if deletion strategy config exists
        deletion_strategy = db.query(Config).filter(Config.key == "deletion_strategy").first()
        if not deletion_strategy:
            db.add(Config(key="deletion_strategy", value="recycle_bin", description="File deletion strategy: recycle_bin or direct_delete"))

        # Default AI model
        default_model = db.query(Config).filter(Config.key == "default_model").first()
        if not default_model:
            db.add(Config(key="default_model", value="", description="Default AI model for downloads/processing"))

        # NAS connection defaults
        nas_defaults = {
            "nas_address": ("", "NAS IP address or hostname (e.g. 192.168.1.100)"),
            "nas_protocol": ("smb", "Connection protocol: smb or nfs"),
            "nas_share": ("", "Shared folder name (e.g. volume1/music)"),
            "nas_username": ("", "NAS login username"),
            "nas_password": ("", "NAS login password"),
            "nas_port": ("445", "SMB port (default 445) or NFS port (default 2049)"),
        }
        for key, (default_val, desc) in nas_defaults.items():
            existing = db.query(Config).filter(Config.key == key).first()
            if not existing:
                db.add(Config(key=key, value=default_val, description=desc))

        db.commit()

        # Auto-mount NAS share if configured
        def get_val(key, default=""):
            c = db.query(Config).filter(Config.key == key).first()
            return c.value if c else default

        nas_addr = get_val("nas_address")
        nas_share = get_val("nas_share")
        if nas_addr and nas_share:
            result = mount_nas(
                nas_addr,
                get_val("nas_protocol", "smb"),
                nas_share,
                get_val("nas_username"),
                get_val("nas_password"),
                get_val("nas_port"),
            )
            if result["success"]:
                nas_root = db.query(Config).filter(Config.key == "nas_root").first()
                if nas_root:
                    setattr(nas_root, 'value', result["mount_point"])
                db.commit()
                print(f"[NAS] Mounted {nas_addr}/{nas_share} -> {result['mount_point']}")
            else:
                print(f"[NAS] Mount failed: {result['message']}")
        
        # Auto-detect dependencies
        ytdlp_status = "installed" if shutil.which("yt-dlp") else "missing"
        ffmpeg_status = "installed" if shutil.which("ffmpeg") else "missing"
        
        # Update or create dependency status configs
        ytdlp_config = db.query(Config).filter(Config.key == "ytdlp_status").first()
        if ytdlp_config:
            setattr(ytdlp_config, 'value', ytdlp_status)
        else:
            db.add(Config(key="ytdlp_status", value=ytdlp_status, description="yt-dlp installation status"))
        
        ffmpeg_config = db.query(Config).filter(Config.key == "ffmpeg_status").first()
        if ffmpeg_config:
            setattr(ffmpeg_config, 'value', ffmpeg_status)
        else:
            db.add(Config(key="ffmpeg_status", value=ffmpeg_status, description="ffmpeg installation status"))
        
        db.commit()

        # Add default sync folder (/Music) if no sync folders configured
        from .models import SyncFolder
        sync_count = db.query(SyncFolder).count()
        if sync_count == 0:
            # Check if /Music exists in NAS root
            music_path = os.path.join(get_nas_root(db), "Music")
            if os.path.isdir(music_path):
                db.add(SyncFolder(path="/Music", name="Music", enabled=True))
                db.commit()
                print("[Index] Added default index folder: /Music")

    finally:
        db.close()

# Include routers
app.include_router(config_router)
app.include_router(files_router)
app.include_router(youtube_router)
app.include_router(audio_router)
app.include_router(sync_router)

# Main routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    db = next(get_db())
    try:
        nas_root = get_nas_root(db)
    finally:
        db.close()
    return templates.TemplateResponse(name="index.html", request=request, context={"request": request, "nas_root": nas_root})

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    db = next(get_db())
    try:
        def get_config_value(key, default=""):
            c = db.query(Config).filter(Config.key == key).first()
            return c.value if c else default

        context = {
            "request": request,
            "nas_root": get_config_value("nas_root", "/nas"),
            "deletion_strategy": get_config_value("deletion_strategy", "recycle_bin"),
            "ytdlp_status": get_config_value("ytdlp_status", "missing"),
            "ffmpeg_status": get_config_value("ffmpeg_status", "missing"),
            "nas_address": get_config_value("nas_address"),
            "nas_protocol": get_config_value("nas_protocol", "smb"),
            "nas_share": get_config_value("nas_share"),
            "nas_username": get_config_value("nas_username"),
            "nas_password": get_config_value("nas_password"),
            "nas_port": get_config_value("nas_port", "445"),
        }

        return templates.TemplateResponse(name="settings.html", request=request, context=context)
    finally:
        db.close()

@app.on_event("shutdown")
async def shutdown_event():
    # Stop download manager
    await download_manager.stop()


@app.get("/health")
async def health():
    return {"status": "ok"}