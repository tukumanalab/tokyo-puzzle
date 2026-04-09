#!/usr/bin/env python3
"""
東京都市区町村パズル 枠（フレーム）STL 生成スクリプト。

断面構造:
  z=5mm ──────┐      ┌──── 枠上面
              │      │
  z=2.5mm     │  ┌───┘──── 境界リッジ上面
  z=2mm ──────┘  ╔══╝──── ピース受け面（底面から2mm、深さ3mm）
                 ║
  z=0mm ─────────╚──────── 底面

  枠壁: 0〜5mm  /  ピースくり抜き: 深さ 3mm  /  境界リッジ: 0.5mm 盛り上がり  /  接続タブ: 5mm 貫通

組み合わせると ~448 × 228 mm の一体枠:
  [ tama_west ~184mm ] | [ tama_east ~125mm ] | [ wards ~148mm ]

Usage:
  python3 scripts/gen_frame.py                  # 全セクション
  python3 scripts/gen_frame.py frame_wards
  python3 scripts/gen_frame.py frame_tama_east
  python3 scripts/gen_frame.py frame_tama_west
"""

import json, math, os, struct, sys
from collections import defaultdict
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation

# ── 座標変換定数 ────────────────────────────────────────────────────────────
PROJ_CENTER_LAT = 35.65
PROJ_CENTER_LON = 139.45
METERS_PER_DEGREE = 111320.0
COS_CENTER = math.cos(PROJ_CENTER_LAT * math.pi / 180)
XY_SCALE = 1.5 / 300
MAINLAND_BBOX = dict(minLon=138.85, maxLon=139.98, minLat=35.38, maxLat=35.92)

# ── フレーム仕様 ────────────────────────────────────────────────────────────
FRAME_THICK  = 4.0   # 枠全体の高さ (mm)
BASE_HEIGHT  = 2.0   # ピース受け底面の高さ (mm); ポケット深さ = FRAME_THICK - BASE_HEIGHT = 3mm
RIDGE_HEIGHT = 0.5   # 市区町村境界線の盛り上がり高さ (mm)
CLEARANCE    = 0.3   # ピースくり抜きクリアランス (mm)
MARGIN       = 3.0   # 外周マージン (mm)
PX_SIZE      = 0.5   # ラスタ解像度 (mm/px)

# ── タブ接続仕様 ────────────────────────────────────────────────────────────
TAB_D_MM   = 5.0     # タブの奥行き (mm, x 方向)
TAB_H_MM   = 15.0    # タブの高さ (mm, y 方向)
GAP_H_MM   = 10.0    # タブ間の隙間 (mm)
TAB_CLR_MM = 0.3     # スロットのクリアランス (mm, 上下各辺)

# ── セクション境界 x 座標 (mm) ──────────────────────────────────────────────
SEAM1_X_MM = -53.0   # 多摩西部/中部境界
SEAM2_X_MM = +67.0   # 多摩中部/23区境界（全多摩市区 max_x +66mm + 1mm）

# ── 市区町村コード ──────────────────────────────────────────────────────────
WARD_CODES = [
    '13101','13102','13103','13104','13105','13106','13107','13108','13109','13110',
    '13111','13112','13113','13114','13115','13116','13117','13118','13119','13120',
    '13121','13122','13123',
]
TAMA_WEST_CODES = [
    '13201','13205','13227','13228','13303','13305','13307','13308',
]
TAMA_EAST_CODES = [
    '13202','13203','13204','13206','13207','13208','13209','13210',
    '13211','13212','13213','13214','13215','13218','13219','13220',
    '13221','13222','13223','13224','13225','13229',
]
ALL_CODES = TAMA_WEST_CODES + TAMA_EAST_CODES + WARD_CODES

SECTIONS = {
    'frame_tama_west': {
        'label': '多摩地区西部・西多摩郡',
        'seam_left':  None,
        'seam_right': SEAM1_X_MM,
    },
    'frame_tama_east': {
        'label': '多摩地区中部',
        'seam_left':  SEAM1_X_MM,
        'seam_right': SEAM2_X_MM,
    },
    'frame_wards': {
        'label': '特別区（23区）',
        'seam_left':  SEAM2_X_MM,
        'seam_right': None,
    },
}

STL_TRI = np.dtype([('n','<3f4'),('v0','<3f4'),('v1','<3f4'),('v2','<3f4'),('a','<u2')])

def lon_lat_to_mm(lon, lat):
    x = (lon - PROJ_CENTER_LON) * COS_CENTER * METERS_PER_DEGREE * XY_SCALE
    y = (lat - PROJ_CENTER_LAT) * METERS_PER_DEGREE * XY_SCALE
    return x, y

def ring_area(ring):
    n = len(ring)
    a = 0.0
    for i in range(n):
        j = (i + 1) % n
        a += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1]
    return abs(a) / 2.0

def find_main_component(candidates, coord_decimals=5):
    n = len(candidates)
    if n == 0:
        return set()
    coord_to_polys = defaultdict(list)
    for i, (_, rings) in enumerate(candidates):
        for pt in rings[0]:
            key = (round(pt[0], coord_decimals), round(pt[1], coord_decimals))
            coord_to_polys[key].append(i)
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(x, y):
        px, py = find(x), find(y)
        if px != py: parent[px] = py
    for polys in coord_to_polys.values():
        if len(polys) > 1:
            for o in polys[1:]: union(polys[0], o)
    comp_area = defaultdict(float)
    for i, (area, _) in enumerate(candidates): comp_area[find(i)] += area
    main_root = max(comp_area, key=comp_area.__getitem__)
    return {i for i in range(n) if find(i) == main_root}

def feature_to_polygons(feature):
    geom = feature['geometry']
    b = MAINLAND_BBOX
    candidates = []
    def add_poly(coords):
        rings = [[(p[0], p[1]) for p in ring] for ring in coords]
        outer = rings[0]
        cx = sum(p[0] for p in outer) / len(outer)
        cy = sum(p[1] for p in outer) / len(outer)
        if b['minLon'] <= cx <= b['maxLon'] and b['minLat'] <= cy <= b['maxLat']:
            candidates.append((ring_area(outer), rings))
    if geom['type'] == 'Polygon':
        add_poly(geom['coordinates'])
    elif geom['type'] == 'MultiPolygon':
        for poly in geom['coordinates']: add_poly(poly)
    if not candidates:
        return []
    main_idx = find_main_component(candidates)
    excluded = len(candidates) - len(main_idx)
    if excluded:
        print(f'    飛び地除外: {excluded} ポリゴン')
    return [rings for i, (_, rings) in enumerate(candidates) if i in main_idx]

def make_tris(p0, p1, p2):
    e1 = p1 - p0; e2 = p2 - p0
    n = np.cross(e1, e2)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    ln = np.where(ln > 0, ln, 1.0)
    n = (n / ln).astype(np.float32)
    out = np.zeros(len(p0), dtype=STL_TRI)
    out['n'] = n
    out['v0'] = p0.astype(np.float32)
    out['v1'] = p1.astype(np.float32)
    out['v2'] = p2.astype(np.float32)
    return out

def write_stl(path, tri_arrays):
    all_tris = np.concatenate([t for t in tri_arrays if len(t) > 0])
    with open(path, 'wb') as f:
        f.write(b'\x00' * 80)
        f.write(struct.pack('<I', len(all_tris)))
        f.write(all_tris.tobytes())
    return len(all_tris)


# ── フレームマスク計算 ─────────────────────────────────────────────────────
def compute_municipality_mask(codes, boundary_dir, g_x_min, g_y_min, g_h,
                               col_start, col_end):
    """
    市区町村ポリゴンのラスタマスクを返す。

    Returns:
        pocket_mask : True = ピースポケット (クリアランス拡張済み)
        ridge_mask  : True = 市区町村境界線 (pocket_mask のサブセット)
    """
    section_w = col_end - col_start
    h = g_h
    label_map = np.zeros((h, section_w), dtype=np.int32)
    hole_mask = np.zeros((h, section_w), dtype=bool)

    for idx, code in enumerate(codes, 1):
        path = os.path.join(boundary_dir, f'{code}.json')
        with open(path) as f:
            feat = json.load(f)
        polys = feature_to_polygons(feat)
        img = Image.new('L', (section_w, h), 0)
        draw = ImageDraw.Draw(img)
        for rings in polys:
            outer = rings[0]
            pts = []
            for lon, lat in outer:
                xm, ym = lon_lat_to_mm(lon, lat)
                px_c = (xm - g_x_min) / PX_SIZE - col_start
                px_r = h - 1 - (ym - g_y_min) / PX_SIZE
                pts.append((px_c, px_r))
            if len(pts) >= 3:
                draw.polygon(pts, fill=255)
        msk = np.array(img) > 128
        label_map[msk] = idx
        hole_mask |= msk

    # 境界検出: 異なる自治体に隣接するピクセル
    ridge_mask = np.zeros((h, section_w), dtype=bool)
    for dr, dc in [(-1, 0), (+1, 0), (0, -1), (0, +1)]:
        shifted = np.roll(np.roll(label_map, -dr, axis=0), -dc, axis=1)
        ridge_mask |= (label_map > 0) & (shifted > 0) & (label_map != shifted)

    # クリアランス拡張（ポケットのみ）
    clr_px = max(1, round(CLEARANCE / PX_SIZE))
    pocket_mask = binary_dilation(hole_mask,
                                  structure=np.ones((2*clr_px+1, 2*clr_px+1), dtype=bool))

    # リッジは未拡張の自治体領域内のみ
    ridge_mask = ridge_mask & hole_mask

    return pocket_mask, ridge_mask


# ── タブパターン生成 ────────────────────────────────────────────────────────
def make_tab_pattern(h_px, valid_rows):
    tab_h = round(TAB_H_MM / PX_SIZE)
    gap_h = round(GAP_H_MM  / PX_SIZE)
    pattern = np.zeros(h_px, dtype=bool)
    r = 0
    while r < h_px:
        pattern[r:r + tab_h] = True
        r += tab_h + gap_h
    return pattern & valid_rows


# ── STL メッシュ生成 ──────────────────────────────────────────────────────
def masks_to_stl(frame_mask, pocket_mask, ridge_mask, x_min, g_y_min, g_h, path):
    """
    frame_mask  : True = 枠素材 (z=0 〜 FRAME_THICK の柱)
    pocket_mask : True = ピースポケット底面 (z=0 〜 BASE_HEIGHT の台 + 上部開口)
    ridge_mask  : True = 境界リッジ (z=0 〜 BASE_HEIGHT+RIDGE_HEIGHT の台 + 上部開口)
                  ridge_mask は pocket_mask のサブセット
    それ以外    : 貫通穴 (タブスロット等)

    生成される面:
      bottom (z=0)              : frame + pocket (ridge 含む) 全ピクセル
      top frame (z=FRAME_THICK) : frame のみ
      top pocket (z=BASE_HEIGHT): pocket & ~ridge のみ
      top ridge  (z=BASE_HEIGHT+RIDGE_HEIGHT): ridge のみ
      外周壁                    : z=0 〜 FRAME_THICK
      ポケット内壁              : z=BASE_HEIGHT 〜 FRAME_THICK (frame/pocket 境界)
      リッジ内壁（frame 側）    : z=BASE_HEIGHT+RIDGE_HEIGHT 〜 FRAME_THICK
      リッジステップ壁          : z=BASE_HEIGHT 〜 BASE_HEIGHT+RIDGE_HEIGHT (ridge/pocket 境界)
    """
    rows, cols = frame_mask.shape
    h = g_h
    tris = []
    zT = np.float32(FRAME_THICK)
    zB = np.float32(BASE_HEIGHT)
    zR = np.float32(BASE_HEIGHT + RIDGE_HEIGHT)
    z0 = np.float32(0.0)

    normal_pocket = pocket_mask & ~ridge_mask  # floor at BASE_HEIGHT

    # ── bottom face (z=0) : frame + pocket ─────────────────────────────
    bot_mask = frame_mask | pocket_mask
    R, C = np.where(bot_mask)
    n = len(R)
    x0 = (x_min + C * PX_SIZE).astype(np.float32)
    x1 = x0 + np.float32(PX_SIZE)
    y0 = (g_y_min + (h - R - 1) * PX_SIZE).astype(np.float32)
    y1 = y0 + np.float32(PX_SIZE)
    z_b = np.full(n, z0, np.float32)
    A2 = np.stack([x0, y0, z_b], axis=1); B2 = np.stack([x1, y0, z_b], axis=1)
    C2 = np.stack([x0, y1, z_b], axis=1); D2 = np.stack([x1, y1, z_b], axis=1)
    t = make_tris(A2, C2, B2); t['n'] = (0, 0, -1); tris.append(t)
    t = make_tris(B2, C2, D2); t['n'] = (0, 0, -1); tris.append(t)

    # ── top face of frame (z=FRAME_THICK) ──────────────────────────────
    R, C = np.where(frame_mask)
    n = len(R)
    x0 = (x_min + C * PX_SIZE).astype(np.float32)
    x1 = x0 + np.float32(PX_SIZE)
    y0 = (g_y_min + (h - R - 1) * PX_SIZE).astype(np.float32)
    y1 = y0 + np.float32(PX_SIZE)
    z_t = np.full(n, zT, np.float32)
    A = np.stack([x0, y0, z_t], axis=1); B = np.stack([x1, y0, z_t], axis=1)
    C_ = np.stack([x0, y1, z_t], axis=1); D = np.stack([x1, y1, z_t], axis=1)
    t = make_tris(A, B, C_); t['n'] = (0, 0, 1); tris.append(t)
    t = make_tris(B, D, C_); t['n'] = (0, 0, 1); tris.append(t)

    # ── top face of normal pocket (z=BASE_HEIGHT) ──────────────────────
    R, C = np.where(normal_pocket)
    n = len(R)
    x0 = (x_min + C * PX_SIZE).astype(np.float32)
    x1 = x0 + np.float32(PX_SIZE)
    y0 = (g_y_min + (h - R - 1) * PX_SIZE).astype(np.float32)
    y1 = y0 + np.float32(PX_SIZE)
    z_p = np.full(n, zB, np.float32)
    A = np.stack([x0, y0, z_p], axis=1); B = np.stack([x1, y0, z_p], axis=1)
    C_ = np.stack([x0, y1, z_p], axis=1); D = np.stack([x1, y1, z_p], axis=1)
    t = make_tris(A, B, C_); t['n'] = (0, 0, 1); tris.append(t)
    t = make_tris(B, D, C_); t['n'] = (0, 0, 1); tris.append(t)

    # ── top face of ridge (z=BASE_HEIGHT + RIDGE_HEIGHT) ───────────────
    R, C = np.where(ridge_mask)
    n = len(R)
    if n > 0:
        x0 = (x_min + C * PX_SIZE).astype(np.float32)
        x1 = x0 + np.float32(PX_SIZE)
        y0 = (g_y_min + (h - R - 1) * PX_SIZE).astype(np.float32)
        y1 = y0 + np.float32(PX_SIZE)
        z_r = np.full(n, zR, np.float32)
        A = np.stack([x0, y0, z_r], axis=1); B = np.stack([x1, y0, z_r], axis=1)
        C_ = np.stack([x0, y1, z_r], axis=1); D = np.stack([x1, y1, z_r], axis=1)
        t = make_tris(A, B, C_); t['n'] = (0, 0, 1); tris.append(t)
        t = make_tris(B, D, C_); t['n'] = (0, 0, 1); tris.append(t)

    # ── walls ───────────────────────────────────────────────────────────
    def walls_for_frame(dr, dc):
        """
        frame ピクセルの各辺を走査し、隣接状態に応じて壁を生成。
          隣 = frame             → 壁なし
          隣 = normal_pocket     → z=BASE_HEIGHT 〜 FRAME_THICK
          隣 = ridge             → z=BASE_HEIGHT+RIDGE_HEIGHT 〜 FRAME_THICK
          隣 = slot/outside      → z=0 〜 FRAME_THICK
        """
        R_f, C_f = np.where(frame_mask)
        nr = R_f + dr; nc = C_f + dc
        oob = (nr < 0) | (nr >= rows) | (nc < 0) | (nc >= cols)
        nr_s = np.clip(nr, 0, rows-1); nc_s = np.clip(nc, 0, cols-1)
        nbr_frame         = ~oob & frame_mask[nr_s, nc_s]
        nbr_normal_pocket = ~oob & normal_pocket[nr_s, nc_s]
        nbr_ridge         = ~oob & ridge_mask[nr_s, nc_s]
        nbr_slot_or_out   = ~nbr_frame & ~nbr_normal_pocket & ~nbr_ridge

        part_tris = []

        # ポケット内壁 (z=BASE_HEIGHT 〜 FRAME_THICK)
        m = nbr_normal_pocket
        if m.any():
            r = R_f[m]; c = C_f[m]; nw = m.sum()
            xi0 = (x_min + c * PX_SIZE).astype(np.float32)
            xi1 = xi0 + np.float32(PX_SIZE)
            yi0 = (g_y_min + (h-r-1) * PX_SIZE).astype(np.float32)
            yi1 = yi0 + np.float32(PX_SIZE)
            zt = np.full(nw, zT, np.float32); zb = np.full(nw, zB, np.float32)
            part_tris.append(_quad(dr, dc, xi0, xi1, yi0, yi1, zt, zb))

        # リッジ隣接壁 (z=BASE_HEIGHT+RIDGE_HEIGHT 〜 FRAME_THICK)
        m = nbr_ridge
        if m.any():
            r = R_f[m]; c = C_f[m]; nw = m.sum()
            xi0 = (x_min + c * PX_SIZE).astype(np.float32)
            xi1 = xi0 + np.float32(PX_SIZE)
            yi0 = (g_y_min + (h-r-1) * PX_SIZE).astype(np.float32)
            yi1 = yi0 + np.float32(PX_SIZE)
            zt = np.full(nw, zT, np.float32); zb = np.full(nw, zR, np.float32)
            part_tris.append(_quad(dr, dc, xi0, xi1, yi0, yi1, zt, zb))

        # 貫通壁 (z=0 〜 FRAME_THICK)
        m = nbr_slot_or_out
        if m.any():
            r = R_f[m]; c = C_f[m]; nw = m.sum()
            xi0 = (x_min + c * PX_SIZE).astype(np.float32)
            xi1 = xi0 + np.float32(PX_SIZE)
            yi0 = (g_y_min + (h-r-1) * PX_SIZE).astype(np.float32)
            yi1 = yi0 + np.float32(PX_SIZE)
            zt = np.full(nw, zT, np.float32); zb_full = np.full(nw, z0, np.float32)
            part_tris.append(_quad(dr, dc, xi0, xi1, yi0, yi1, zt, zb_full))

        return part_tris

    def walls_for_ridge(dr, dc):
        """
        ridge ピクセルの各辺を走査し、隣接 normal_pocket 側にステップ壁を生成。
          隣 = normal_pocket → z=BASE_HEIGHT 〜 BASE_HEIGHT+RIDGE_HEIGHT
        """
        R_r, C_r = np.where(ridge_mask)
        if len(R_r) == 0:
            return []
        nr = R_r + dr; nc = C_r + dc
        oob = (nr < 0) | (nr >= rows) | (nc < 0) | (nc >= cols)
        nr_s = np.clip(nr, 0, rows-1); nc_s = np.clip(nc, 0, cols-1)
        nbr_normal_pocket = ~oob & normal_pocket[nr_s, nc_s]

        m = nbr_normal_pocket
        if not m.any():
            return []
        r = R_r[m]; c = C_r[m]; nw = m.sum()
        xi0 = (x_min + c * PX_SIZE).astype(np.float32)
        xi1 = xi0 + np.float32(PX_SIZE)
        yi0 = (g_y_min + (h-r-1) * PX_SIZE).astype(np.float32)
        yi1 = yi0 + np.float32(PX_SIZE)
        zt = np.full(nw, zR, np.float32)
        zb = np.full(nw, zB, np.float32)
        return [_quad(dr, dc, xi0, xi1, yi0, yi1, zt, zb)]

    def walls_for_pocket_base(dr, dc):
        """
        pocket/ridge ピクセルが slot/outside に面する場合の側壁を生成。
        通常は frame に囲まれるため不要だが、tab slot 隣接時や section 境界で必要。
          normal_pocket 隣接 open → z=0 〜 BASE_HEIGHT
          ridge 隣接 open         → z=0 〜 BASE_HEIGHT+RIDGE_HEIGHT
        """
        part_tris = []
        for mask, top_z in [(normal_pocket, zB), (ridge_mask, zR)]:
            R_p, C_p = np.where(mask)
            if len(R_p) == 0:
                continue
            nr = R_p + dr; nc = C_p + dc
            oob = (nr < 0) | (nr >= rows) | (nc < 0) | (nc >= cols)
            nr_s = np.clip(nr, 0, rows-1); nc_s = np.clip(nc, 0, cols-1)
            nbr_frame  = ~oob & frame_mask[nr_s, nc_s]
            nbr_pocket = ~oob & pocket_mask[nr_s, nc_s]
            nbr_open   = ~nbr_frame & ~nbr_pocket
            m = nbr_open
            if not m.any():
                continue
            r = R_p[m]; c = C_p[m]; nw = m.sum()
            xi0 = (x_min + c * PX_SIZE).astype(np.float32)
            xi1 = xi0 + np.float32(PX_SIZE)
            yi0 = (g_y_min + (h-r-1) * PX_SIZE).astype(np.float32)
            yi1 = yi0 + np.float32(PX_SIZE)
            zt_arr = np.full(nw, top_z, np.float32)
            zb_arr = np.full(nw, z0, np.float32)
            part_tris.append(_quad(dr, dc, xi0, xi1, yi0, yi1, zt_arr, zb_arr))
        return part_tris

    for dr, dc in [(-1, 0), (+1, 0), (0, -1), (0, +1)]:
        tris.extend(walls_for_frame(dr, dc))
        tris.extend(walls_for_ridge(dr, dc))
        tris.extend(walls_for_pocket_base(dr, dc))

    total = write_stl(path, tris)
    mb = os.path.getsize(path) / (1024**2)
    return total, mb


def _quad(dr, dc, xi0, xi1, yi0, yi1, zt, zb):
    """指定方向の壁クワッドを生成（法線はくり抜き/外部方向）。"""
    if dr == -1:
        pa_t = np.stack([xi0, yi1, zt], axis=1); pb_t = np.stack([xi1, yi1, zt], axis=1)
        pa_b = np.stack([xi0, yi1, zb], axis=1); pb_b = np.stack([xi1, yi1, zb], axis=1)
    elif dr == +1:
        pa_t = np.stack([xi1, yi0, zt], axis=1); pb_t = np.stack([xi0, yi0, zt], axis=1)
        pa_b = np.stack([xi1, yi0, zb], axis=1); pb_b = np.stack([xi0, yi0, zb], axis=1)
    elif dc == -1:
        pa_t = np.stack([xi0, yi0, zt], axis=1); pb_t = np.stack([xi0, yi1, zt], axis=1)
        pa_b = np.stack([xi0, yi0, zb], axis=1); pb_b = np.stack([xi0, yi1, zb], axis=1)
    else:
        pa_t = np.stack([xi1, yi1, zt], axis=1); pb_t = np.stack([xi1, yi0, zt], axis=1)
        pa_b = np.stack([xi1, yi1, zb], axis=1); pb_b = np.stack([xi1, yi0, zb], axis=1)
    t1 = make_tris(pa_t, pb_t, pa_b)
    t2 = make_tris(pb_t, pb_b, pa_b)
    return np.concatenate([t1, t2])


def main():
    args = sys.argv[1:]
    keys = args if args else list(SECTIONS.keys())
    for key in keys:
        if key not in SECTIONS:
            print(f'不明なセクション: {key}  (有効: {list(SECTIONS.keys())})')
            sys.exit(1)

    base_dir     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    boundary_dir = os.path.join(base_dir, 'public', 'data', 'boundary')
    out_dir      = os.path.join(base_dir, 'public', 'data', 'stl')
    os.makedirs(out_dir, exist_ok=True)

    # ── グローバル bbox ────────────────────────────────────────────────
    print('グローバル bbox 計算中...')
    all_xs, all_ys = [], []
    for code in ALL_CODES:
        path = os.path.join(boundary_dir, f'{code}.json')
        with open(path) as f:
            feat = json.load(f)
        geom = feat['geometry']
        rings_flat = geom['coordinates'] if geom['type'] == 'Polygon' else [r for p in geom['coordinates'] for r in p]
        for ring in rings_flat:
            for pt in ring:
                x, y = lon_lat_to_mm(pt[0], pt[1])
                all_xs.append(x); all_ys.append(y)

    g_x_min = min(all_xs) - MARGIN; g_x_max = max(all_xs) + MARGIN
    g_y_min = min(all_ys) - MARGIN; g_y_max = max(all_ys) + MARGIN
    g_w = int(math.ceil((g_x_max - g_x_min) / PX_SIZE))
    g_h = int(math.ceil((g_y_max - g_y_min) / PX_SIZE))
    print(f'グローバルグリッド: {g_w} × {g_h} px = {g_w*PX_SIZE:.0f} × {g_h*PX_SIZE:.0f} mm')

    def x_to_col(x_mm):
        return int(round((x_mm - g_x_min) / PX_SIZE))

    seam1_col = x_to_col(SEAM1_X_MM)
    seam2_col = x_to_col(SEAM2_X_MM)
    section_cols = {
        'frame_tama_west': (0, seam1_col),
        'frame_tama_east': (seam1_col, seam2_col),
        'frame_wards':     (seam2_col, g_w),
    }

    tab_d_px = round(TAB_D_MM  / PX_SIZE)
    clr_px   = max(1, round(TAB_CLR_MM / PX_SIZE))

    # ── フェーズ 1: 市区町村マスク計算 ────────────────────────────────
    print('\nフェーズ1: 市区町村マスク計算...')
    muni_masks  = {}   # pocket_mask
    ridge_masks = {}   # ridge_mask
    for key in SECTIONS:
        if key not in keys:
            continue
        col_s, col_e = section_cols[key]
        print(f'  {key}: {(col_e-col_s)*PX_SIZE:.0f} mm 幅')
        pm, rm = compute_municipality_mask(
            ALL_CODES, boundary_dir, g_x_min, g_y_min, g_h, col_s, col_e)
        muni_masks[key]  = pm
        ridge_masks[key] = rm

    # ── フェーズ 2: タブ/スロット追加 ─────────────────────────────────
    print('\nフェーズ2: タブ/スロット追加...')
    frame_masks  = {k: ~v for k, v in muni_masks.items()}
    pocket_masks = {k: v.copy() for k, v in muni_masks.items()}
    # ridge_masks はフェーズ1で既に dict に入っている

    def add_tab_slot(key_l, key_r):
        if key_l not in frame_masks or key_r not in frame_masks:
            return
        fm_l  = frame_masks[key_l]
        fm_r  = frame_masks[key_r]
        pm_r  = pocket_masks[key_r]
        rm_r  = ridge_masks[key_r]
        h = fm_l.shape[0]

        valid_l = fm_l[:, -1]
        valid_r = fm_r[:, 0]
        valid_both = valid_l & valid_r
        tab_rows = make_tab_pattern(h, valid_both)
        print(f'  {key_l}→{key_r}: タブ行数={tab_rows.sum()} ({tab_rows.sum()*PX_SIZE:.0f}mm)')

        # タブ: frame_l の右に tab_d_px 列追加
        tab_cols_fm = np.zeros((h, tab_d_px), dtype=bool)
        tab_cols_fm[tab_rows, :] = True
        frame_masks[key_l]  = np.hstack([fm_l, tab_cols_fm])
        pocket_masks[key_l] = np.hstack([pocket_masks[key_l],
                                          np.zeros((h, tab_d_px), dtype=bool)])
        ridge_masks[key_l]  = np.hstack([ridge_masks[key_l],
                                          np.zeros((h, tab_d_px), dtype=bool)])

        # スロット: frame_r の左端をくり抜く（貫通穴 = pocket も ridge も False）
        clr_struct = np.ones((2*clr_px+1, 1), dtype=bool)
        slot_rows_exp = binary_dilation(
            tab_rows.reshape(-1, 1), structure=clr_struct).ravel()
        slot_depth = tab_d_px + clr_px
        for d in range(slot_depth):
            if d < fm_r.shape[1]:
                fm_r[slot_rows_exp, d]  = False
                pm_r[slot_rows_exp, d]  = False
                rm_r[slot_rows_exp, d]  = False
        frame_masks[key_r]  = fm_r
        pocket_masks[key_r] = pm_r
        ridge_masks[key_r]  = rm_r

    add_tab_slot('frame_tama_west', 'frame_tama_east')
    add_tab_slot('frame_tama_east', 'frame_wards')

    # ── フェーズ 3: STL 生成 ──────────────────────────────────────────
    print('\nフェーズ3: STL 生成...')
    for key in keys:
        if key not in frame_masks:
            continue
        col_s, _ = section_cols[key]
        x_min_sec = g_x_min + col_s * PX_SIZE
        fm = frame_masks[key]
        pm = pocket_masks[key]
        rm = ridge_masks[key]
        out_path = os.path.join(out_dir, f'{key}.stl')
        total, mb = masks_to_stl(fm, pm, rm, x_min_sec, g_y_min, g_h, out_path)
        w_mm = fm.shape[1] * PX_SIZE
        print(f'  {key}: {w_mm:.0f} × {g_h*PX_SIZE:.0f} mm  →  {total:,} tri, {mb:.1f} MB')

    print('\n全完了。')


if __name__ == '__main__':
    main()
