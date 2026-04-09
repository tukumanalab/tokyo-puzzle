#!/usr/bin/env python3
"""
東京都市区町村の STL ファイルを生成するスクリプト。
public/data/boundary/{code}.json の境界データと国土地理院 DEM を使用。
public/data/stl/{code}.stl に出力する。

Usage:
  python3 scripts/gen_stl.py              # 全市区町村
  python3 scripts/gen_stl.py 13101        # 千代田区のみ
  python3 scripts/gen_stl.py --dec 2 13101

縦方向倍率: 1 倍（Z_SCALE=1.0、都道府県パズルの 5 倍に対して実寸比に近い設定）
裏面: 市区町村名のみ（コードなし）
"""
import json, math, os, struct, sys, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np

# ── 定数 ──────────────────────────────────────────────────────────────────
PROJ_CENTER_LAT = 35.65
PROJ_CENTER_LON = 139.45
METERS_PER_DEGREE = 111320.0
COS_CENTER = math.cos(PROJ_CENTER_LAT * math.pi / 180)
TILE_SIZE = 256
DEM_TILE_URL = 'https://cyberjapandata.gsi.go.jp/xyz/dem/{z}/{x}/{y}.txt'

# 東京本土の範囲（島嶼除外フィルタ用）
MAINLAND_BBOX = dict(minLon=138.85, maxLon=139.98, minLat=35.38, maxLat=35.92)

# デフォルトパラメータ
ZOOM       = 13        # zoom13 で約 10m 解像度（市区町村レベルに適切）
XY_SCALE   = 1.5 / 300    # 市区町村スケール（都道府県の5.5倍）
# ※ 都道府県パズルの XY_SCALE=1.5/1660 では千代田区が約3.7mm角になるため拡大。
# 1.5/300 ≈ 0.005 mm/m → 東京全体 約400mm幅・千代田区 約20mm角
Z_SCALE    = 1.0       # 縦方向倍率 1 倍（実寸比に近い）
BASE_THICK = 3.0       # ベース厚さ (mm)
DECIMATION = 2         # zoom13 では decimation=2 が適切（~20m 解像度）
CLEARANCE_PX = 4       # 境界クリアランス（zoom13 では小さめに）
BBOX_PAD   = 0.015     # 境界ファイルから bbox を計算する際のパディング（度）

CODES = [
    # 特別区
    '13101','13102','13103','13104','13105','13106','13107','13108','13109','13110',
    '13111','13112','13113','13114','13115','13116','13117','13118','13119','13120',
    '13121','13122','13123',
    # 多摩地区 市
    '13201','13202','13203','13204','13205','13206','13207','13208','13209','13210',
    '13211','13212','13213','13214','13215','13218','13219','13220','13221','13222',
    '13223','13224','13225','13227','13228','13229',
    # 西多摩郡 町村
    '13303','13305','13307','13308',
]

# 大面積の市は zoom を下げて処理を軽くする
MUNICIPALITY_PARAMS = {
    '13201': dict(zoom=12, decimation=4),   # 八王子市（大）
    '13205': dict(zoom=12, decimation=4),   # 青梅市（大）
    '13228': dict(zoom=12, decimation=4),   # あきる野市（大）
    '13307': dict(zoom=12, decimation=4),   # 檜原村（山岳）
    '13308': dict(zoom=12, decimation=4),   # 奥多摩町（最大・山岳）
    '13209': dict(zoom=12, decimation=4),   # 町田市（大）
}

# ── 彫刻設定 ──────────────────────────────────────────────────────────────
ENGRAVE_DEPTH   = 1.5   # 彫り深さ (mm)
ENGRAVE_TEXT_MM = 6.0   # テキスト行高さ (mm)

# ── タイル座標変換 ─────────────────────────────────────────────────────────
def lon_to_tile_x(lon, z): return int((lon + 180) / 360 * (2**z))
def lat_to_tile_y(lat, z):
    lr = lat * math.pi / 180
    return int((1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * (2**z))
def tile_to_nw(x, y, z):
    n = 2**z
    lon = x / n * 360 - 180
    lat = math.atan(math.sinh(math.pi * (1 - 2 * y / n))) * 180 / math.pi
    return lon, lat

# ── タイル読み込み ─────────────────────────────────────────────────────────
def parse_dem_txt(text):
    data = np.full(TILE_SIZE * TILE_SIZE, np.nan, dtype=np.float32)
    for r, row in enumerate(text.strip().split('\n')[:TILE_SIZE]):
        for c, v in enumerate(row.split(',')[:TILE_SIZE]):
            v = v.strip()
            if v and v != 'e':
                try: data[r * TILE_SIZE + c] = float(v)
                except ValueError: pass
    return data

def load_tile(z, x, y, dem_dir):
    bin_path = os.path.join(dem_dir, str(z), str(x), f'{y}.bin')
    if os.path.exists(bin_path):
        raw = np.frombuffer(open(bin_path, 'rb').read(), dtype='<f4')
        return raw.copy()
    url = DEM_TILE_URL.format(z=z, x=x, y=y)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = parse_dem_txt(r.read().decode())
        os.makedirs(os.path.dirname(bin_path), exist_ok=True)
        data.astype('<f4').tofile(bin_path)
        return data
    except (urllib.error.HTTPError, Exception):
        return np.full(TILE_SIZE * TILE_SIZE, np.nan, dtype=np.float32)

# ── DEM グリッド取得 ───────────────────────────────────────────────────────
def fetch_dem_grid(bbox, dem_dir, zoom=None):
    z = zoom if zoom is not None else ZOOM
    xm = lon_to_tile_x(bbox['minLon'], z)
    xM = lon_to_tile_x(bbox['maxLon'], z)
    ym = lat_to_tile_y(bbox['maxLat'], z)
    yM = lat_to_tile_y(bbox['minLat'], z)
    nX, nY = xM - xm + 1, yM - ym + 1
    cols, rows = nX * TILE_SIZE, nY * TILE_SIZE
    values = np.full((rows, cols), np.nan, dtype=np.float32)

    tasks = [(tx, ty) for ty in range(ym, yM+1) for tx in range(xm, xM+1)]
    total = len(tasks)
    done = [0]
    def _load(tx, ty):
        tile = load_tile(z, tx, ty, dem_dir).reshape(TILE_SIZE, TILE_SIZE)
        ox, oy = (tx - xm) * TILE_SIZE, (ty - ym) * TILE_SIZE
        return tx, ty, tile, ox, oy
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_load, tx, ty): (tx, ty) for tx, ty in tasks}
        for fut in as_completed(futs):
            tx, ty, tile, ox, oy = fut.result()
            values[oy:oy+TILE_SIZE, ox:ox+TILE_SIZE] = tile
            done[0] += 1
            print(f'\r  タイル {done[0]}/{total}', end='', flush=True)
    print()

    nw_lon, nw_lat = tile_to_nw(xm, ym, z)
    se_lon, se_lat = tile_to_nw(xM+1, yM+1, z)
    bbox_out = dict(minLon=nw_lon, maxLon=se_lon, minLat=se_lat, maxLat=nw_lat)
    return bbox_out, values

# ── 境界 BBox の動的計算 ─────────────────────────────────────────────────
def compute_bbox(geometry):
    """境界 GeoJSON の geometry から DEM 取得用 bbox を算出（パディング付き）。"""
    all_lons, all_lats = [], []
    def collect(geom):
        t = geom['type']
        if t == 'Polygon':
            for ring in geom['coordinates']:
                for pt in ring:
                    all_lons.append(pt[0]); all_lats.append(pt[1])
        elif t == 'MultiPolygon':
            for poly in geom['coordinates']:
                for ring in poly:
                    for pt in ring:
                        all_lons.append(pt[0]); all_lats.append(pt[1])
    collect(geometry)
    pad = BBOX_PAD
    return dict(
        minLon=min(all_lons) - pad,
        maxLon=max(all_lons) + pad,
        minLat=min(all_lats) - pad,
        maxLat=max(all_lats) + pad,
    )

# ── ポリゴン面積（Shoelace 法） ────────────────────────────────────────────
def ring_area(ring):
    n = len(ring)
    a = 0.0
    for i in range(n):
        j = (i + 1) % n
        a += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1]
    return abs(a) / 2.0

# ── 孤立ポリゴン検出 ───────────────────────────────────────────────────────
def find_main_component(candidates, coord_decimals=5):
    from collections import defaultdict
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
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py
    for polys in coord_to_polys.values():
        if len(polys) > 1:
            first = polys[0]
            for other in polys[1:]:
                union(first, other)
    comp_area = defaultdict(float)
    for i, (area, _) in enumerate(candidates):
        comp_area[find(i)] += area
    main_root = max(comp_area, key=comp_area.__getitem__)
    return {i for i in range(n) if find(i) == main_root}

# ── ポリゴン抽出 ──────────────────────────────────────────────────────────
def feature_to_polygons(feature):
    geom = feature['geometry']
    b = MAINLAND_BBOX
    candidates = []
    def add_poly(coords):
        rings = [[(p[0], p[1]) for p in ring] for ring in coords]
        outer = rings[0]
        cx = sum(p[0] for p in outer) / len(outer)
        cy = sum(p[1] for p in outer) / len(outer)
        if cx < b['minLon'] or cx > b['maxLon'] or cy < b['minLat'] or cy > b['maxLat']:
            return
        candidates.append((ring_area(outer), rings))
    if geom['type'] == 'Polygon':
        add_poly(geom['coordinates'])
    elif geom['type'] == 'MultiPolygon':
        for poly in geom['coordinates']:
            add_poly(poly)
    if not candidates:
        return []
    main_idx = find_main_component(candidates)
    excluded = len(candidates) - len(main_idx)
    if excluded:
        print(f'  飛び地除外: {excluded} ポリゴン')
    return [rings for i, (_, rings) in enumerate(candidates) if i in main_idx]

# ── ポリゴン塗りつぶしクリッピング ────────────────────────────────────────
def clip_dem(bbox, values, polygons):
    from PIL import Image, ImageDraw
    rows, cols = values.shape
    lon_step = (bbox['maxLon'] - bbox['minLon']) / cols
    lat_step = (bbox['maxLat'] - bbox['minLat']) / rows
    mask_img = Image.new('L', (cols, rows), 0)
    draw = ImageDraw.Draw(mask_img)
    for poly in polygons:
        outer = poly[0]
        pixels = [
            ((p[0] - bbox['minLon']) / lon_step,
             (bbox['maxLat'] - p[1]) / lat_step)
            for p in outer
        ]
        if len(pixels) >= 3:
            draw.polygon(pixels, fill=255)
    mask = np.array(mask_img) > 0
    return np.where(mask, values, np.nan)

# ── 境界クリアランス ──────────────────────────────────────────────────────
def apply_clearance(clipped, px):
    if px <= 0:
        return clipped
    valid = (~np.isnan(clipped)).astype(np.uint8)
    eroded = valid.copy()
    for _ in range(px):
        eroded &= np.roll(eroded,  1, axis=0)
        eroded &= np.roll(eroded, -1, axis=0)
        eroded &= np.roll(eroded,  1, axis=1)
        eroded &= np.roll(eroded, -1, axis=1)
        eroded[0, :] = 0; eroded[-1, :] = 0
        eroded[:, 0] = 0; eroded[:, -1] = 0
    result = clipped.copy()
    result[eroded == 0] = np.nan
    return result

# ── テキスト彫刻 ──────────────────────────────────────────────────────────
def find_jp_font():
    candidates = [
        '/System/Library/Fonts/AquaKana.ttc',
        '/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
        '/System/Library/Fonts/Hiragino Sans GB.ttc',
        '/System/Library/Fonts/Supplemental/AppleGothic.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def make_text_mask(values, text_lines, font_path, px_per_mm, font_size_mm=ENGRAVE_TEXT_MM):
    """底面中央にテキストマスクを生成する（裏面から読めるよう左右ミラー）。"""
    from PIL import Image, ImageDraw, ImageFont
    rows, cols = values.shape
    valid = ~np.isnan(values)
    if not valid.any() or font_path is None:
        return np.zeros((rows, cols), dtype=bool), 0
    font_size = max(8, int(font_size_mm * px_per_mm))
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        print('  警告: フォント読み込み失敗。テキスト彫刻スキップ。')
        return np.zeros((rows, cols), dtype=bool), 0
    dummy = ImageDraw.Draw(Image.new('L', (1, 1)))
    line_boxes = [dummy.textbbox((0, 0), l, font=font) for l in text_lines]
    pad = font_size // 4
    img_w = max(b[2] - b[0] for b in line_boxes) + pad * 2
    img_h = sum(b[3] - b[1] for b in line_boxes) + pad * (len(text_lines) + 1)
    img = Image.new('L', (img_w, img_h), 0)
    draw = ImageDraw.Draw(img)
    y = pad
    for line, box in zip(text_lines, line_boxes):
        draw.text((pad, y), line, font=font, fill=255)
        y += (box[3] - box[1]) + pad
    img = img.transpose(Image.FLIP_LEFT_RIGHT)  # 裏面から読めるようミラー
    img_arr = np.array(img) > 128
    img_h2, img_w2 = img_arr.shape
    r_coords, c_coords = np.where(valid)
    row_c = int(np.median(r_coords))
    col_c = int(np.median(c_coords))
    r0 = row_c - img_h2 // 2
    c0 = col_c - img_w2 // 2
    mask = np.zeros((rows, cols), dtype=bool)
    r_s = max(0, r0); r_e = min(rows, r0 + img_h2)
    c_s = max(0, c0); c_e = min(cols, c0 + img_w2)
    placed = img_arr[r_s - r0:r_e - r0, c_s - c0:c_e - c0]
    mask[r_s:r_e, c_s:c_e] = placed
    pre_clip_count = int(placed.sum())
    mask &= valid
    return mask, pre_clip_count

def fit_text_mask(values, text_lines, font_path, px_per_mm,
                  max_mm=ENGRAVE_TEXT_MM, min_mm=3.0, steps=8):
    """有効エリアに完全に収まる最大フォントサイズを二分探索して返す。"""
    lo, hi = min_mm, max_mm
    best_mask = None
    best_mm = min_mm
    for _ in range(steps):
        mid = (lo + hi) / 2
        mask, pre = make_text_mask(values, text_lines, font_path, px_per_mm, mid)
        if pre > 0 and int(mask.sum()) == pre:
            best_mask, best_mm = mask, mid
            lo = mid
        else:
            hi = mid
    if best_mask is None:
        best_mask, _ = make_text_mask(values, text_lines, font_path, px_per_mm, min_mm)
        best_mm = min_mm
    return best_mask, best_mm

def pool_mask(mask, dec):
    rows, cols = mask.shape
    hpad = (-rows) % dec
    wpad = (-cols) % dec
    if hpad or wpad:
        mask = np.pad(mask, ((0, hpad), (0, wpad)))
    h2, w2 = mask.shape
    return mask.reshape(h2 // dec, dec, w2 // dec, dec).any(axis=(1, 3))

# ワールド座標スケール（gen_one で上書き可能）
_CUR_XY_SCALE = XY_SCALE

# ── ワールド座標グリッド ───────────────────────────────────────────────────
def world_grid(bbox, values):
    rows, cols = values.shape
    lon_step = (bbox['maxLon'] - bbox['minLon']) / cols
    lat_step = (bbox['maxLat'] - bbox['minLat']) / rows
    c_idx = np.arange(cols, dtype=np.float32)
    r_idx = np.arange(rows, dtype=np.float32)
    lons = bbox['minLon'] + (c_idx + 0.5) * lon_step
    lats = bbox['maxLat'] - (r_idx + 0.5) * lat_step
    lons2d, lats2d = np.meshgrid(lons, lats)
    s = _CUR_XY_SCALE
    wx = ((lons2d - PROJ_CENTER_LON) * COS_CENTER * METERS_PER_DEGREE * s).astype(np.float32)
    wy = ((lats2d - PROJ_CENTER_LAT) * METERS_PER_DEGREE * s).astype(np.float32)
    wz = np.where(np.isnan(values), np.nan, (values * Z_SCALE * s).astype(np.float32))
    return wx, wy, wz

# ── STL 型 ────────────────────────────────────────────────────────────────
STL_TRI = np.dtype([('n','<3f4'),('v0','<3f4'),('v1','<3f4'),('v2','<3f4'),('a','<u2')])

def _norms(e1, e2):
    n = np.cross(e1, e2)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    ln = np.where(ln > 0, ln, 1.0)
    return (n / ln).astype(np.float32)

def make_tris(p0, p1, p2):
    e1 = p1 - p0; e2 = p2 - p0
    n = _norms(e1, e2)
    out = np.zeros(len(p0), dtype=STL_TRI)
    out['n'] = n; out['v0'] = p0; out['v1'] = p1; out['v2'] = p2
    return out

# ── 地形メッシュ ──────────────────────────────────────────────────────────
def build_terrain(bbox, values, dec):
    rows, cols = values.shape
    wx, wy, wz = world_grid(bbox, values)
    sea_z = np.float32(0.0)
    wz_f = np.where(np.isnan(wz), sea_z, wz).astype(np.float32)

    valid_z = wz_f[~np.isnan(wz)]
    min_valid_z = float(valid_z.min()) if len(valid_z) else 0.0
    base_z = min(min_valid_z, 0.0) - BASE_THICK

    R, C = np.meshgrid(np.arange(0, rows-dec, dec), np.arange(0, cols-dec, dec), indexing='ij')
    R2 = np.minimum(R + dec, rows-1)
    C2 = np.minimum(C + dec, cols-1)

    v00 = values[R, C]; v10 = values[R2, C]; v01 = values[R, C2]; v11 = values[R2, C2]
    m = ~(np.isnan(v00) & np.isnan(v10) & np.isnan(v01) & np.isnan(v11))
    R=R[m]; C=C[m]; R2=R2[m]; C2=C2[m]

    def xyz(r, c): return np.stack([wx[r,c], wy[r,c], wz_f[r,c]], axis=1)
    A=xyz(R,C); B=xyz(R2,C); C_=xyz(R,C2); D=xyz(R2,C2)

    t1 = make_tris(A, C_, B)
    t2 = make_tris(B, C_, D)
    return np.concatenate([t1, t2]), base_z

# ── 壁メッシュ ────────────────────────────────────────────────────────────
def _wall_quads(x1, y1, z1, x2, y2, z2, bz):
    nx = -(y2 - y1); ny = x2 - x1
    ln = np.sqrt(nx**2 + ny**2); ln = np.where(ln > 0, ln, 1.0)
    nx /= ln; ny /= ln
    nz = np.zeros_like(nx)
    bz_arr = np.full(len(x1), bz, dtype=np.float32)
    p1t = np.stack([x1, y1, z1], axis=1).astype(np.float32)
    p2t = np.stack([x2, y2, z2], axis=1).astype(np.float32)
    p1b = np.stack([x1, y1, bz_arr], axis=1).astype(np.float32)
    p2b = np.stack([x2, y2, bz_arr], axis=1).astype(np.float32)
    nm  = np.stack([nx, ny, nz], axis=1).astype(np.float32)
    N = len(x1)
    out = np.zeros(N * 2, dtype=STL_TRI)
    out['n'][:N] = nm; out['v0'][:N] = p1t; out['v1'][:N] = p2t; out['v2'][:N] = p1b
    out['n'][N:] = nm; out['v0'][N:] = p2t; out['v1'][N:] = p2b; out['v2'][N:] = p1b
    return out

def build_walls(bbox, values, base_z, dec):
    rows, cols = values.shape
    wx, wy, wz = world_grid(bbox, values)
    sea_z = np.float32(0.0)
    wz_f = np.where(np.isnan(wz), sea_z, wz).astype(np.float32)
    bz = np.float32(base_z)

    R, C = np.meshgrid(np.arange(0, rows, dec), np.arange(0, cols, dec), indexing='ij')
    R2 = np.minimum(R + dec, rows-1)
    C2 = np.minimum(C + dec, cols-1)
    valid = ~np.isnan(values[R, C])

    def nbr_invalid(dr, dc):
        nr = R + dr; nc = C + dc
        oob = (nr < 0) | (nr >= rows) | (nc < 0) | (nc >= cols)
        nr_s = np.clip(nr, 0, rows-1); nc_s = np.clip(nc, 0, cols-1)
        return valid & (oob | np.isnan(values[nr_s, nc_s]))

    parts = []
    m = nbr_invalid(-dec, 0)
    if m.any():
        r,c,c2 = R[m],C[m],C2[m]
        parts.append(_wall_quads(wx[r,c2],wy[r,c2],wz_f[r,c2], wx[r,c],wy[r,c],wz_f[r,c], bz))
    m = nbr_invalid(dec, 0)
    if m.any():
        r2,c,c2 = R2[m],C[m],C2[m]
        parts.append(_wall_quads(wx[r2,c],wy[r2,c],wz_f[r2,c], wx[r2,c2],wy[r2,c2],wz_f[r2,c2], bz))
    m = nbr_invalid(0, -dec)
    if m.any():
        r,r2,c = R[m],R2[m],C[m]
        parts.append(_wall_quads(wx[r,c],wy[r,c],wz_f[r,c], wx[r2,c],wy[r2,c],wz_f[r2,c], bz))
    m = nbr_invalid(0, dec)
    if m.any():
        r,r2,c2 = R[m],R2[m],C2[m]
        parts.append(_wall_quads(wx[r2,c2],wy[r2,c2],wz_f[r2,c2], wx[r,c2],wy[r,c2],wz_f[r,c2], bz))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=STL_TRI)

# ── 底面メッシュ ──────────────────────────────────────────────────────────
def build_bottom(bbox, values, base_z, dec, text_mask=None):
    rows, cols = values.shape
    wx, wy, _ = world_grid(bbox, values)
    bz_bg  = np.float32(base_z)
    bz_txt = np.float32(base_z + ENGRAVE_DEPTH)

    R, C = np.meshgrid(np.arange(0, rows-dec, dec), np.arange(0, cols-dec, dec), indexing='ij')
    R2 = np.minimum(R + dec, rows-1)
    C2 = np.minimum(C + dec, cols-1)

    v00 = values[R,C]; v10 = values[R2,C]; v01 = values[R,C2]; v11 = values[R2,C2]
    m = ~(np.isnan(v00) & np.isnan(v10) & np.isnan(v01) & np.isnan(v11))
    R=R[m]; C=C[m]; R2=R2[m]; C2=C2[m]

    if text_mask is not None:
        bz_cell = np.where(text_mask[R // dec, C // dec], bz_txt, bz_bg).astype(np.float32)
    else:
        bz_cell = np.full(len(R), bz_bg, dtype=np.float32)

    def xyz_bot(r, c): return np.stack([wx[r,c], wy[r,c], bz_cell], axis=1).astype(np.float32)
    A=xyz_bot(R,C); B=xyz_bot(R2,C); C_=xyz_bot(R,C2); D=xyz_bot(R2,C2)

    t1 = make_tris(A, B, C_)
    t2 = make_tris(B, D, C_)
    tris = np.concatenate([t1, t2])
    tris['n'] = (0.0, 0.0, -1.0)
    return tris

def build_text_walls(bbox, values, base_z, dec, text_mask):
    if text_mask is None or not text_mask.any():
        return np.zeros(0, dtype=STL_TRI)
    rows, cols = values.shape
    wx, wy, _ = world_grid(bbox, values)
    bz_bg  = np.float32(base_z)
    bz_txt = np.float32(base_z + ENGRAVE_DEPTH)

    R, C = np.meshgrid(np.arange(0, rows, dec), np.arange(0, cols, dec), indexing='ij')
    R2 = np.minimum(R + dec, rows-1)
    C2 = np.minimum(C + dec, cols-1)
    valid = ~(np.isnan(values[R, C]) & np.isnan(values[R2, C]) &
              np.isnan(values[R, C2]) & np.isnan(values[R2, C2]))
    tm_rows, tm_cols = text_mask.shape
    is_text = text_mask[R // dec, C // dec]

    def nbr_non_text(dr, dc):
        nr = R + dr; nc = C + dc
        oob = (nr < 0) | (nr >= rows) | (nc < 0) | (nc >= cols)
        nr_s = np.clip(nr, 0, rows-1); nc_s = np.clip(nc, 0, cols-1)
        nr_s2 = np.minimum(nr_s + dec, rows-1); nc_s2 = np.minimum(nc_s + dec, cols-1)
        nr_p = np.clip(nr_s // dec, 0, tm_rows - 1)
        nc_p = np.clip(nc_s // dec, 0, tm_cols - 1)
        nbr_valid = ~(np.isnan(values[nr_s, nc_s]) & np.isnan(values[nr_s2, nc_s]) &
                      np.isnan(values[nr_s, nc_s2]) & np.isnan(values[nr_s2, nc_s2]))
        nbr_is_text = ~oob & text_mask[nr_p, nc_p] & nbr_valid
        return valid & is_text & ~nbr_is_text

    bz_fn = lambda n: np.full(n, bz_txt, dtype=np.float32)
    parts = []
    m = nbr_non_text(-dec, 0)
    if m.any():
        r,c,c2 = R[m],C[m],C2[m]; ba = bz_fn(m.sum())
        parts.append(_wall_quads(wx[r,c],wy[r,c],ba, wx[r,c2],wy[r,c2],ba, bz_bg))
    m = nbr_non_text(dec, 0)
    if m.any():
        r2,c,c2 = R2[m],C[m],C2[m]; ba = bz_fn(m.sum())
        parts.append(_wall_quads(wx[r2,c2],wy[r2,c2],ba, wx[r2,c],wy[r2,c],ba, bz_bg))
    m = nbr_non_text(0, -dec)
    if m.any():
        r,r2,c = R[m],R2[m],C[m]; ba = bz_fn(m.sum())
        parts.append(_wall_quads(wx[r2,c],wy[r2,c],ba, wx[r,c],wy[r,c],ba, bz_bg))
    m = nbr_non_text(0, dec)
    if m.any():
        r,r2,c2 = R[m],R2[m],C2[m]; ba = bz_fn(m.sum())
        parts.append(_wall_quads(wx[r,c2],wy[r,c2],ba, wx[r2,c2],wy[r2,c2],ba, bz_bg))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=STL_TRI)

# ── STL 書き出し ──────────────────────────────────────────────────────────
def write_stl(path, tri_arrays):
    all_tris = np.concatenate([t for t in tri_arrays if len(t) > 0])
    with open(path, 'wb') as f:
        f.write(b'\x00' * 80)
        f.write(struct.pack('<I', len(all_tris)))
        f.write(all_tris.tobytes())

# ── メイン ────────────────────────────────────────────────────────────────
def gen_one(code, base_dir, dec):
    global _CUR_XY_SCALE
    params = MUNICIPALITY_PARAMS.get(code, {})
    _CUR_XY_SCALE = params.get('xy_scale', XY_SCALE)
    pref_zoom = params.get('zoom', None)
    dec = params.get('decimation', dec)

    boundary_path = os.path.join(base_dir, 'public', 'data', 'boundary', f'{code}.json')
    dem_dir       = os.path.join(base_dir, 'public', 'data', 'dem')
    out_dir       = os.path.join(base_dir, 'public', 'data', 'stl')
    out_path      = os.path.join(out_dir, f'{code}.stl')

    print(f'\n=== {code} ===')
    with open(boundary_path) as f:
        feature = json.load(f)

    name = feature['properties'].get('name', code)
    print(f'  名称: {name}')

    bbox = compute_bbox(feature['geometry'])
    print(f'  bbox: lon {bbox["minLon"]:.4f}–{bbox["maxLon"]:.4f}  lat {bbox["minLat"]:.4f}–{bbox["maxLat"]:.4f}')

    print('  DEM 読み込み中...')
    grid_bbox, values = fetch_dem_grid(bbox, dem_dir, zoom=pref_zoom)
    print(f'  グリッド: {values.shape[0]}×{values.shape[1]}')

    print('  クリッピング中...')
    polygons = feature_to_polygons(feature)
    clipped  = clip_dem(grid_bbox, values, polygons)
    clipped  = apply_clearance(clipped, CLEARANCE_PX)
    valid_n  = int(np.sum(~np.isnan(clipped)))
    print(f'  有効セル: {valid_n:,}')

    print('  テキストマスク生成...')
    grid_pixel_lon = (grid_bbox['maxLon'] - grid_bbox['minLon']) / clipped.shape[1]
    grid_pixel_mm  = grid_pixel_lon * COS_CENTER * METERS_PER_DEGREE * _CUR_XY_SCALE
    px_per_mm = 1.0 / grid_pixel_mm
    jp_font   = find_jp_font()

    # 裏面には市区町村名のみ印刷（コードなし）
    # 1行・2行どちらが大きく収まるか試す
    mask_1line, mm_1line = fit_text_mask(clipped, [name],                jp_font, px_per_mm)
    # 名称が3文字以上なら2行分割も試みる（例: 「世田谷」「区」）
    if len(name) >= 4:
        mid = len(name) // 2
        mask_2line, mm_2line = fit_text_mask(clipped, [name[:mid], name[mid:]], jp_font, px_per_mm)
    else:
        mask_2line, mm_2line = mask_1line, mm_1line

    if mm_1line >= mm_2line:
        text_mask, used_mm, layout = mask_1line, mm_1line, '1行'
    else:
        text_mask, used_mm, layout = mask_2line, mm_2line, '2行'
    print(f'  テキスト: {layout} {used_mm:.1f} mm  ピクセル: {text_mask.sum():,}')
    text_mask_pooled = pool_mask(text_mask, dec)

    print('  地形メッシュ生成...')
    terrain_tris, base_z = build_terrain(grid_bbox, clipped, dec)
    print(f'  地形 tri: {len(terrain_tris):,}')

    print('  壁メッシュ生成...')
    wall_tris = build_walls(grid_bbox, clipped, base_z, dec)
    print(f'  壁 tri: {len(wall_tris):,}')

    print('  底面メッシュ生成...')
    bot_tris = build_bottom(grid_bbox, clipped, base_z, dec, text_mask_pooled)
    print(f'  底面 tri: {len(bot_tris):,}')

    print('  テキスト壁生成...')
    txt_wall_tris = build_text_walls(grid_bbox, clipped, base_z, dec, text_mask_pooled)
    print(f'  テキスト壁 tri: {len(txt_wall_tris):,}')

    os.makedirs(out_dir, exist_ok=True)
    write_stl(out_path, [terrain_tris, wall_tris, bot_tris, txt_wall_tris])
    mb = os.path.getsize(out_path) / (1024**2)
    total = len(terrain_tris) + len(wall_tris) + len(bot_tris)
    print(f'  完了: {out_path}  ({total:,} tri, {mb:.1f} MB)')

def main():
    args = sys.argv[1:]
    dec = DECIMATION
    codes = []
    i = 0
    while i < len(args):
        if args[i] == '--dec' and i + 1 < len(args):
            dec = int(args[i+1]); i += 2
        else:
            codes.append(args[i]); i += 1
    if not codes:
        codes = CODES

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f'decimation={dec}  zoom={ZOOM}  Z_SCALE={Z_SCALE}')
    for code in codes:
        gen_one(code, base_dir, dec)
    print('\n全完了。')

if __name__ == '__main__':
    main()
