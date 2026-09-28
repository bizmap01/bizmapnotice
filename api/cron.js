export default async function handler(req, res) {
  try {
    // 🔒 이 엔드포인트는 외부에서 아무나 호출하면 크롤러가 무단으로 실행될 수 있다.
    // Vercel Cron이 호출할 때는 CRON_SECRET을 Authorization 헤더에 자동으로 실어서 보내주므로
    // (Vercel 공식 동작, 별도 설정 불필요), 여기서 그 값만 검증하면 다른 사람은 호출할 수 없다.
    const cronSecret = process.env.CRON_SECRET;
    if (!cronSecret) {
      return res.status(500).json({ success: false, message: 'CRON_SECRET 환경 변수가 설정되지 않았습니다.' });
    }
    if (req.headers.authorization !== `Bearer ${cronSecret}`) {
      return res.status(401).json({ success: false, message: '인증되지 않은 요청입니다.' });
    }

    const token = process.env.GH_TOKEN;
    
    if (!token) {
      return res.status(500).json({ success: false, message: 'GH_TOKEN 환경 변수가 설정되지 않았습니다.' });
    }

    const response = await fetch('https://api.github.com/repos/bizmap01/bizmapnotice/actions/workflows/crawler.yml/dispatches', {
      method: 'POST',
      headers: {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': `Bearer ${token}`,
        'User-Agent': 'Bizmap-Cron',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ ref: 'main' })
    });

    if (response.ok) {
      return res.status(200).json({ success: true, message: '크롤러 정상 호출 완료' });
    } else {
      const errorText = await response.text();
      return res.status(response.status).json({ success: false, error: errorText });
    }
  } catch (error) {
    return res.status(500).json({ success: false, message: error.message });
  }
}
