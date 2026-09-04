// api/cancel.js
export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ message: 'Method Not Allowed' });
  }

  const { customer_uid } = req.body;

  if (!customer_uid) {
    return res.status(400).json({ success: false, message: 'customer_uid 누락' });
  }

  try {
    // 1. 포트원 인증 토큰 발급
    const tokenRes = await fetch('https://api.iamport.kr/users/getToken', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        imp_key: '1135816288587000',
        imp_secret: 'bllNTF6BztOjhIJBeDJULl4oSK2v9SlFK60VQcJSBdcr82YLzOuNeKL0FflE7RiWqRGUy7CLXC6NuG2e'
      })
    });

    const tokenData = await tokenRes.json();
    if (tokenData.code !== 0) {
      return res.status(400).json({ success: false, message: '토큰 발급 실패: ' + tokenData.message });
    }

    const accessToken = tokenData.response.access_token;

    // 2. 포트원 예약 결제 스케줄 취소
    const cancelRes = await fetch('https://api.iamport.kr/subscribe/payments/unschedule', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`
      },
      body: JSON.stringify({ customer_uid: customer_uid })
    });

    const cancelData = await cancelRes.json();

    return res.status(200).json({
      success: true,
      message: '예약 취소 처리 완료',
      response: cancelData.response
    });
  } catch (err) {
    return res.status(500).json({ success: false, message: '서버 오류: ' + err.message });
  }
}
