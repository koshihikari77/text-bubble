from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from bubble.assets import pick_font_path, resolve_bubble_asset
from bubble.editor_models import (
    add_workspace_case,
    export_case_document,
    initialize_project,
    list_bubble_types,
    load_case_document,
    load_project,
    render_case_document,
    rendered_path_for_case,
    resolve_document_image,
    save_case_document,
)
from bubble.editor_render import render_single_bubble_sprite
from bubble.scene_runtime import RenderConfig


STATIC_DIR = Path(__file__).resolve().parent / "editor_static"


def _render_config_from_document(document: dict[str, Any]) -> RenderConfig:
    settings = document.get("render", {})
    bubble_asset_name = settings.get("bubble_asset")
    bubble_asset = None
    if bubble_asset_name:
        bubble_asset = resolve_bubble_asset(str(bubble_asset_name))
        if bubble_asset is None:
            raise RuntimeError(f"bubble asset not found: {bubble_asset_name}")
    return RenderConfig(
        font_path=pick_font_path(settings.get("font")),
        font_family=settings.get("font_family"),
        bubble_asset=bubble_asset,
        font_size=int(settings.get("font_size", 0) or 0),
        text_renderer=str(settings.get("text_renderer", "resvg-hybrid")),
        bubble_renderer=str(settings.get("bubble_renderer", "resvg")),
        text_letter_spacing=str(settings.get("text_letter_spacing", "-1px")),
        text_word_spacing=str(settings.get("text_word_spacing", "0")),
        resvg_tu_override=bool(settings.get("resvg_tu_override", True)),
    )


def _image_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def create_editor_app(project_dir: Path) -> Any:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, Response
    from fastapi.staticfiles import StaticFiles

    root = project_dir.resolve()
    initialize_project(root)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    api = FastAPI(title="text-bubble editor")

    def _case_document_or_404(case_id: str) -> dict[str, Any]:
        try:
            return load_case_document(root, case_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _bubble_or_404(document: dict[str, Any], bubble_id: str) -> dict[str, Any]:
        for bubble in document["bubbles"]:
            if bubble["bubble_id"] == bubble_id:
                return bubble
        raise HTTPException(status_code=404, detail=f"bubble not found: {bubble_id}")

    @api.get("/api/project")
    def get_project() -> dict[str, Any]:
        return load_project(root)

    @api.get("/api/bubble-types")
    def get_bubble_types() -> dict[str, Any]:
        return {"types": list_bubble_types()}

    @api.get("/api/cases/{case_id}/document")
    def get_document(case_id: str) -> dict[str, Any]:
        return _case_document_or_404(case_id)

    @api.put("/api/cases/{case_id}/document")
    def put_document(case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return save_case_document(root, case_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/cases/{case_id}/export")
    def export_document(case_id: str) -> dict[str, Any]:
        try:
            return {"paths": export_case_document(root, case_id)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/cases/{case_id}/render")
    def render_document(case_id: str) -> dict[str, Any]:
        try:
            output_path = render_case_document(root, case_id)
            return {"rendered": str(output_path)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/cases/import-workspace")
    def import_workspace(payload: dict[str, Any]) -> dict[str, Any]:
        case_id = payload.get("case_id")
        workspace = payload.get("workspace")
        image = payload.get("image")
        if not isinstance(case_id, str) or not case_id.strip():
            raise HTTPException(status_code=400, detail="case_id is required")
        if not isinstance(workspace, str) or not workspace.strip():
            raise HTTPException(status_code=400, detail="workspace is required")
        try:
            document = add_workspace_case(
                project_dir=root,
                case_id=case_id.strip(),
                workspace=Path(workspace),
                image_path=Path(image) if isinstance(image, str) and image.strip() else None,
            )
            return document
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/cases/{case_id}/image")
    def get_case_image(case_id: str) -> FileResponse:
        document = _case_document_or_404(case_id)
        image_path = resolve_document_image(document, project_dir=root)
        if not image_path.exists():
            raise HTTPException(status_code=404, detail=f"image not found: {image_path}")
        return FileResponse(image_path)

    @api.get("/api/cases/{case_id}/image-info")
    def get_case_image_info(case_id: str) -> dict[str, Any]:
        document = _case_document_or_404(case_id)
        image_path = resolve_document_image(document, project_dir=root)
        if not image_path.exists():
            raise HTTPException(status_code=404, detail=f"image not found: {image_path}")
        width, height = _image_dimensions(image_path)
        return {"width": width, "height": height, "path": str(image_path)}

    @api.get("/api/cases/{case_id}/rendered")
    def get_case_rendered(case_id: str) -> FileResponse:
        _case_document_or_404(case_id)
        output_path = rendered_path_for_case(root, case_id)
        if not output_path.exists():
            raise HTTPException(status_code=404, detail="rendered image is not available")
        return FileResponse(output_path)

    @api.get("/api/cases/{case_id}/rendered-status")
    def get_case_rendered_status(case_id: str) -> dict[str, Any]:
        _case_document_or_404(case_id)
        output_path = rendered_path_for_case(root, case_id)
        return {"available": output_path.exists(), "path": str(output_path)}

    @api.get("/api/cases/{case_id}/bubbles/{bubble_id}/sprite-info")
    def get_sprite_info(case_id: str, bubble_id: str) -> dict[str, Any]:
        document = _case_document_or_404(case_id)
        bubble = _bubble_or_404(document, bubble_id)
        image_path = resolve_document_image(document, project_dir=root)
        if not image_path.exists():
            raise HTTPException(status_code=404, detail=f"image not found: {image_path}")
        canvas_width, canvas_height = _image_dimensions(image_path)
        try:
            sprite = render_single_bubble_sprite(
                bubble=bubble,
                canvas_width=canvas_width,
                canvas_height=canvas_height,
                render_config=_render_config_from_document(document),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "bubble_id": bubble_id,
            "width_px": sprite.width_px,
            "height_px": sprite.height_px,
            "anchor_offset_x": sprite.anchor_offset_x,
            "anchor_offset_y": sprite.anchor_offset_y,
            "version_hash": sprite.version_hash,
        }

    @api.get("/api/cases/{case_id}/bubbles/{bubble_id}/sprite")
    def get_sprite_png(case_id: str, bubble_id: str) -> Response:
        document = _case_document_or_404(case_id)
        bubble = _bubble_or_404(document, bubble_id)
        image_path = resolve_document_image(document, project_dir=root)
        if not image_path.exists():
            raise HTTPException(status_code=404, detail=f"image not found: {image_path}")
        canvas_width, canvas_height = _image_dimensions(image_path)
        try:
            sprite = render_single_bubble_sprite(
                bubble=bubble,
                canvas_width=canvas_width,
                canvas_height=canvas_height,
                render_config=_render_config_from_document(document),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        headers = {
            "Cache-Control": "no-cache, must-revalidate",
            "X-Sprite-Width": str(sprite.width_px),
            "X-Sprite-Height": str(sprite.height_px),
            "X-Sprite-Anchor-X": str(sprite.anchor_offset_x),
            "X-Sprite-Anchor-Y": str(sprite.anchor_offset_y),
            "X-Sprite-Version": sprite.version_hash,
        }
        return Response(content=sprite.png_bytes, media_type="image/png", headers=headers)

    @api.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return api


def run_editor_server(*, project_dir: Path, host: str, port: int) -> None:
    import uvicorn

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_editor_app(project_dir), host=host, port=port)
