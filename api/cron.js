export default async function handler(req, res) {
  try {
    const response = await fetch('https://api.github.com/repos/bizmap01/bizmapnotice/actions/workflows/crawler.yml/dispatches', {
      method: 'POST',
      headers: {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': 'Bearer ghp_rjj6zLkCUTWHZNM90cwQIloGJqXYnL1X2DfD',
        'User-Agent': 'Bizmap-Cron',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ ref: 'main' })
    });

    if (response.ok) {
      return res.status(200).json({ success: true, message: '크롤러 정상 호출 완료' });
    } else {
      const errorText = await response.text();
      return res.status(500).json({ success: false, error: errorText });
    }
  } catch (error) {
    return res.status(500).json({ success: false, message: error.message });
  }
}
