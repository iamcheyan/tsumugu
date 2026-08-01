# AGENTS.md - Codebase Patterns and Conventions

## General Patterns

### FastAPI + Jinja2 + HTMX Architecture
- FastAPI serves as the backend with Jinja2 templates for HTML rendering
- HTMX handles dynamic updates without full page reloads
- Templates use component-based architecture with reusable partials in `app/templates/components/`
- Static files served from `app/static/` (CSS, JS)

### Database Patterns
- SQLAlchemy ORM with SQLite database (`data.db`)
- Config stored in `config` table with key-value pairs
- Use `setattr()` for Column assignment to maintain mypy compatibility
- Database tables auto-created on startup

### Template Patterns
- Base template (`base.html`) defines layout with blocks: `title`, `content`, `scripts`
- Child templates extend base and override blocks
- HTMX attributes: `hx-get`, `hx-target`, `hx-swap`, `hx-trigger`
- Component templates in `components/` directory for reusable UI elements

### Type Safety
- Use mypy with `--ignore-missing-imports` for type checking
- Explicit type annotations required for complex structures
- SQLAlchemy Column types need explicit casting to native types
- Cast dictionary values to native types (e.g., `str(x["name"])`) in sort functions to avoid mypy errors

## File Structure

```
app/
├── main.py                 # FastAPI app, startup events, main routes
├── database.py            # SQLAlchemy setup, session management
├── models.py              # SQLAlchemy models (Config, DownloadHistory, FileMetadata)
├── paths.py               # Shared NAS-root/path confinement helpers
├── ws_broadcast.py        # Shared websocket message helpers
├── download_manager.py    # Download task manager (wget/yt-dlp, worker loop)
├── compressor.py          # Audio compression task manager
├── ai_rename.py           # AI filename analysis (LLM providers)
├── audio_splitter.py      # Chapter/silence-based audio splitting
├── sync_service.py        # MD5 file index generation for device sync
├── nas_mount.py           # SMB/NFS mount/unmount helpers
├── routers/
│   ├── __init__.py
│   ├── config.py          # Config management endpoints
│   ├── files.py           # File browsing endpoints (tree, list, metadata)
│   ├── youtube.py         # YouTube download + preview endpoints
│   ├── audio.py           # Audio streaming/player/split endpoints
│   └── sync.py            # Sync folder + file-index endpoints
├── templates/
│   ├── base.html          # Base layout template
│   ├── index.html         # Home page
│   ├── settings.html      # Settings page
│   └── components/        # Reusable template components
│       ├── tree.html      # Directory tree structure
│       ├── tree_node.html # Individual tree node
│       ├── tree_children.html # Lazy-loaded children
│       └── file_list.html # File list display with table layout
└── static/
    ├── css/
    │   └── style.css      # Dark theme and layout styles
    └── js/
        └── main.js        # Basic JavaScript utilities
```

## File List Patterns
- File list endpoints return HTML via TemplateResponse for HTMX integration
- Use custom Jinja2 filters for formatting (e.g., filesizeformat)
- Add custom filters via `templates.env.filters['filter_name'] = filter_function`
- Sort directories first in file lists for better UX
- File type detection uses file extensions (e.g., .mp3, .wav, .flac for audio)

## Common Gotchas

### HTMX Route Ordering
- Specific routes must come before parameterized routes
- Example: `/tree/children` before `/{key}`

### SQLAlchemy Column Access
- Use `getattr(obj, 'column')` or `obj.column` for reading
- Use `setattr(obj, 'column', value)` for assignment to avoid mypy errors

### TemplateResponse Usage
- Import from `fastapi.responses`, not directly from `fastapi`
- Signature: `templates.TemplateResponse(name=..., request=..., context={})`

### File System Operations
- Always handle `PermissionError` when accessing directories
- Use `os.makedirs(path, exist_ok=True)` for directory creation
- Check `os.path.exists()` before operations
- **Every endpoint that touches the filesystem must resolve user-supplied
  paths through `paths.resolve_within_nas(db, path)`** (realpath-boundary
  check, raises 403 on escape). Never join `nas_root` with user input
  directly; read the root via `paths.get_nas_root(db)`, not an inline
  `Config("nas_root")` lookup

### HTMX ID Naming
- Use predictable IDs for targeting (e.g., `tree-children-/path`)
- Ensure unique IDs across the DOM

### Event Parameter Passing
- When calling functions from onclick attributes, pass `event` explicitly
- Example: `onclick="selectDirectory('path', event)"`
- In function definition: `function selectDirectory(path, event)`
- Access event.currentTarget to get the clicked element

## File Operations Patterns

### HTMX Form Submissions
- Use `application/x-www-form-urlencoded` for HTMX form submissions with FastAPI `Form()` parameters
- For JSON payloads, use `hx-vals='js:JSON.stringify({...})'` and `hx-headers='{"Content-Type": "application/json"}'`
- Always include `hx-swap="none"` for operations that trigger manual refresh

### Context Menu Implementation
- Context menus need proper event handling to avoid conflicts with other click handlers
- Use `event.preventDefault()` to prevent default browser context menu
- Hide menu when clicking elsewhere with `document.addEventListener('click', hideContextMenu)`
- Store target state (path, type, name) in JavaScript variables for operations

### Drag-and-Drop
- Use `draggable="true"` attribute on draggable elements
- Implement `ondragstart`, `ondragover`, `ondrop` event handlers
- Store source path in `event.dataTransfer.setData('text/plain', path)`
- Handle drop on folder elements only (check `data-type` attribute)
- Add visual feedback with CSS classes (`.dragging`, `.drag-over`)

### File Operations State Management
- Use JavaScript variables for clipboard state (cut/copy/paste)
- Store current operation target (path, type, name) for context menu actions
- Refresh file list after operations using `refreshFileList()` function
- Use HTMX `hx-get` to reload file list with current parameters

### Modal Dialogs
- Create modal dialogs with CSS positioning (`position: fixed`)
- Use `display: flex` for centering and show/hide with `display: none`
- Handle keyboard shortcuts (Escape to close)
- Clean up input fields after dialog closes

### Recycle Bin Implementation
- Store recycle bin path in NAS root directory (`.recycle_bin`)
- Use timestamp prefix to avoid name conflicts: `{timestamp}_{original_name}`
- Move files with `shutil.move()` instead of delete
- Check deletion strategy from config before operations

### Download Bar Patterns
- Fixed positioned elements require adjusting body padding and container height
- Use `position: fixed` with `z-index` for persistent UI elements
- Calculate container height with `calc(100vh - bar_height)` to avoid overlap
- YouTube URL validation can be done client-side with regex patterns
- Conditional form sections can be toggled with JavaScript and CSS `display` property
- Save location should update dynamically when directory selection changes
- Form validation with `onsubmit` handler can prevent submission of invalid data
- Split options (auto-split, split mode, keep original) should be grouped and conditionally shown
- Use `data-*` attributes to store state for dynamic form elements
- Override global functions (like selectDirectory) carefully to avoid breaking existing functionality

### Audio Splitting Patterns
- Use ffprobe with `-show_chapters` flag to extract chapter metadata from audio files
- ffmpeg `silencedetect` filter outputs `silence_end` times to stderr for silence-based splitting
- Use `libmp3lame` codec with VBR quality 2 for good balance of size and quality
- Name split files with pattern: `{base_name} - Track {number:02d}.mp3` for consistency
- Handle chapter titles by sanitizing filenames (remove invalid characters, truncate if needed)
- Minimum track duration threshold (e.g., 10 seconds) prevents splitting into tiny fragments
- File discovery after splitting requires pattern matching since original filename may not be predictable
- Splitting status should be tracked separately from download/conversion status in the UI
- Real-time progress updates via WebSocket should include splitting status for user feedback

### Audio Player Patterns
- Use `StreamingResponse` with file iteration for audio streaming endpoint
- HTMX can load dynamic components into specific DOM elements using `hx-get` and `hx-target`
- Audio player should be loaded via HTMX to avoid page reloads
- Progress bar scrubbing requires handling mousedown/mousemove/mouseup events for drag functionality
- Audio element events: `loadedmetadata` for duration, `timeupdate` for progress, `ended` for completion
- Player panel should expand automatically when audio file is clicked
- Use `data-type="audio"` attribute on file rows for click handling
- FastAPI TemplateResponse requires `name=` parameter, not positional argument
- Always check `os.path.exists()` before streaming files
- Audio files should be served with proper MIME types using `mimetypes.guess_type()`

### Responsive Design Patterns
- Use CSS custom properties (variables) for breakpoints and spacing values
- Mobile breakpoint: ≤768px, Tablet breakpoint: ≤1024px
- Mobile layout: sidebar and player become slide-in overlays with `transform: translateX()`
- Add mobile menu toggle button for sidebar navigation
- Add player toggle button for mobile access to audio player
- Touch-friendly: larger touch targets (min 44px), hide hover effects on touch devices
- Bottom sheet pattern for context menus on mobile (position: fixed, bottom: 0)
- Hide non-essential columns on mobile (size, modified, type) for better readability
- Breadcrumb truncation on mobile shows only last 2 path segments
- Download bar stacks vertically on mobile for better usability
- Video preview card stacks vertically on mobile
- Modal dialogs use 90% width on mobile instead of fixed max-width
- Accessibility: focus-visible styles, prefers-reduced-motion, prefers-contrast: high
- Use `@media (hover: none) and (pointer: coarse)` for touch-specific styles
- Panel state management: separate mobile (open/closed) and desktop (collapsed/expanded) states
- Close opposite panel when opening one on mobile to avoid overlap
- Window resize handler to clean up mobile classes when returning to desktop