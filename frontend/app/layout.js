import './globals.css';

export const metadata = {
  title: 'Slate',
  description: 'Enterprise AI platform — CRM',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
