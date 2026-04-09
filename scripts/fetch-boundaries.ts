import * as fs from 'fs';
import * as path from 'path';
import * as https from 'https';
import * as http from 'http';
import JSZip from 'jszip';

// 東京都本土の市区町村コード（島嶼部を除く）
const MAINLAND_CODES = new Set([
  // 特別区（23区）
  '13101','13102','13103','13104','13105','13106','13107','13108','13109','13110',
  '13111','13112','13113','13114','13115','13116','13117','13118','13119','13120',
  '13121','13122','13123',
  // 多摩地区 市
  '13201','13202','13203','13204','13205','13206','13207','13208','13209','13210',
  '13211','13212','13213','13214','13215','13218','13219','13220','13221','13222',
  '13223','13224','13225','13227','13228','13229',
  // 西多摩郡 町村
  '13303','13305','13307','13308',
]);

const OUT_DIR = path.join(process.cwd(), 'public', 'data', 'boundary');
const N03_URL = 'https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2024/N03-20240101_13_GML.zip';

function download(url: string): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const proto = url.startsWith('https') ? https : http;
    proto.get(url, { timeout: 120000 }, (res) => {
      if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return resolve(download(res.headers.location));
      }
      if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      const chunks: Buffer[] = [];
      res.on('data', (c: Buffer) => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
      res.on('error', reject);
    }).on('error', reject);
  });
}

interface GeoJsonFeature {
  type: 'Feature';
  properties: {
    N03_001: string | null;
    N03_002: string | null;
    N03_003: string | null;
    N03_004: string | null;
    N03_005: string | null;
    N03_007: string | null;
  };
  geometry: {
    type: 'Polygon' | 'MultiPolygon';
    coordinates: number[][][][] | number[][][];
  };
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const missing = [...MAINLAND_CODES].filter(c => !fs.existsSync(path.join(OUT_DIR, `${c}.json`)));
  if (missing.length === 0) {
    console.log('すべての境界ファイルが存在します。スキップ。');
    return;
  }
  console.log(`取得が必要なコード: ${missing.length} 件`);

  console.log('東京都（N03-2024 GeoJSON）をダウンロード中...');
  const buf = await download(N03_URL);
  const zip = await JSZip.loadAsync(buf);

  const geojsonFile = Object.keys(zip.files).find(f => f.endsWith('.geojson'));
  if (!geojsonFile) throw new Error('GeoJSON ファイルが ZIP 内に見つかりません');
  console.log(`  GeoJSON: ${geojsonFile}`);

  const geojsonStr = await zip.files[geojsonFile].async('string');
  const geojson = JSON.parse(geojsonStr) as { features: GeoJsonFeature[] };
  console.log(`  フィーチャ数: ${geojson.features.length}`);

  // N03_007 でポリゴンをグループ化
  const grouped = new Map<string, { rings: number[][][]; name: string }>();

  for (const feat of geojson.features) {
    const code = feat.properties.N03_007;
    if (!code || !MAINLAND_CODES.has(code)) continue;

    const name = feat.properties.N03_004 ?? feat.properties.N03_005 ?? '';
    if (!grouped.has(code)) grouped.set(code, { rings: [], name });

    const geom = feat.geometry;
    if (geom.type === 'Polygon') {
      grouped.get(code)!.rings.push(geom.coordinates as number[][][]);
    } else if (geom.type === 'MultiPolygon') {
      for (const poly of geom.coordinates as number[][][][]) {
        grouped.get(code)!.rings.push(poly);
      }
    }
  }

  console.log(`  本土コード数: ${grouped.size}`);

  for (const [code, { rings, name }] of grouped) {
    const outFile = path.join(OUT_DIR, `${code}.json`);
    if (fs.existsSync(outFile)) {
      console.log(`  ${code}.json 既存、スキップ`);
      continue;
    }
    const feature = {
      type: 'Feature',
      properties: { code, name },
      geometry: { type: 'MultiPolygon', coordinates: rings },
    };
    fs.writeFileSync(outFile, JSON.stringify(feature));
    console.log(`  ${code}.json 書き込み完了 (${name}, ${rings.length} ポリゴン)`);
  }

  for (const code of MAINLAND_CODES) {
    if (!grouped.has(code) && !fs.existsSync(path.join(OUT_DIR, `${code}.json`))) {
      console.warn(`  警告: ${code} のデータが見つかりません`);
    }
  }

  console.log('完了。');
}

main().catch(e => { console.error(e); process.exit(1); });
