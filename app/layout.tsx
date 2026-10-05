import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Локальный Whisper",
  description:
    "Локальная расшифровка русских и английских аудио и видеофайлов.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}

