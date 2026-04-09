import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: '東京都 3D 市区町村パズル',
  description: '東京都の市区町村の地形3Dパズルデータを生成・ダウンロード',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja">
      <body className="bg-gray-950 text-gray-100 min-h-screen">{children}</body>
    </html>
  );
}
