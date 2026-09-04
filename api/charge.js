export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ message: 'Method Not Allowed' });
  }

  const { customer_uid, merchant_uid, amount, name, buyer_email, buyer_name } = req.body;

  if (!customer_uid || !merchant_uid) {
    return res.status(400).json({ success: false, message: '필수 파라미터가 누락되었습니다.' });
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
      return res.status(400).json({ success: false, message: '포트원 토큰 발급 실패: ' + tokenData.message });
    }

    const accessToken = tokenData.response.access_token;

    // 2. 등록된 카드(customer_uid)로 3,900원 출금 승인 요청
    const payRes = await fetch('https://api.iamport.kr/subscribe/payments/again', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`
      },
      body: JSON.stringify({
        customer_uid: customer_uid,
        merchant_uid: merchant_uid,
        amount: amount || 3900,
        name: name || '비즈맵 지원사업 알림 (월간 정기구독)',
        buyer_email: buyer_email,
        buyer_name: buyer_name
      })
    });

    const payData = await payRes.json();

    if (payData.code === 0 && payData.response.status === 'paid') {
      return res.status(200).json({
        success: true,
        imp_uid: payData.response.imp_uid,
        merchant_uid: payData.response.merchant_uid,
        paid_amount: payData.response.amount
      });
    } else {
      return res.status(400).json({
        success: false,
        message: payData.message || '카드 승인 결제 실패'
      });
    }
  } catch (err) {
    return res.status(500).json({ success: false, message: '서버 오류: ' + err.message });
  }
}
