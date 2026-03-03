#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import xml.etree.ElementTree as ET


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Warp a bubble SVG to a target aspect ratio.")
    parser.add_argument("--input", required=True, help="Input SVG path")
    parser.add_argument("--output", required=True, help="Output SVG path")
    parser.add_argument(
        "--target-aspect",
        type=float,
        default=1.0,
        help="Target width/height ratio for the warped SVG viewBox",
    )
    return parser.parse_args()


def parse_viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    raw = root.attrib.get("viewBox")
    if not raw:
        raise RuntimeError("input SVG is missing a viewBox")
    parts = [float(part) for part in raw.replace(",", " ").split()]
    if len(parts) != 4:
        raise RuntimeError(f"invalid viewBox: {raw}")
    return parts[0], parts[1], parts[2], parts[3]


def qname(local: str) -> str:
    return f"{{{SVG_NS}}}{local}"


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    tree = ET.parse(input_path)
    root = tree.getroot()
    vb_x, vb_y, vb_w, vb_h = parse_viewbox(root)
    if vb_w <= 0 or vb_h <= 0:
        raise RuntimeError("viewBox dimensions must be positive")
    if args.target_aspect <= 0:
        raise RuntimeError("target aspect must be positive")

    center_x = vb_x + vb_w / 2.0
    center_y = vb_y + vb_h / 2.0
    target_w = vb_h * args.target_aspect
    target_x = center_x - target_w / 2.0
    scale_x = target_w / vb_w

    defs_nodes: list[ET.Element] = []
    drawable_nodes: list[ET.Element] = []
    for child in list(root):
        if child.tag == qname("defs"):
            defs_nodes.append(copy.deepcopy(child))
        else:
            drawable_nodes.append(copy.deepcopy(child))

    new_root = ET.Element(
        qname("svg"),
        {
            "width": str(int(round(target_w))),
            "height": str(int(round(vb_h))),
            "viewBox": f"{target_x:.4f} {vb_y:.4f} {target_w:.4f} {vb_h:.4f}",
        },
    )
    for defs in defs_nodes:
        new_root.append(defs)

    warp_group = ET.SubElement(
        new_root,
        qname("g"),
        {
            "transform": (
                f"translate({center_x:.4f} {center_y:.4f}) "
                f"scale({scale_x:.6f} 1) "
                f"translate({-center_x:.4f} {-center_y:.4f})"
            )
        },
    )
    for node in drawable_nodes:
        warp_group.append(node)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(new_root).write(output_path, encoding="utf-8", xml_declaration=False)
    print(
        {
            "input": str(input_path),
            "output": str(output_path),
            "source_aspect": round(vb_w / vb_h, 4),
            "target_aspect": round(args.target_aspect, 4),
            "scale_x": round(scale_x, 4),
        }
    )


if __name__ == "__main__":
    main()
