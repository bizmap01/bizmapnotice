// api/charge.js
export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ message: 'Method Not Allowed' });
  }

  // 🔒 [보안수정] 결제 금액(amount)과 상품명(name)을 클라이언트 요청 body에서 그대로 받으면,
  // 브라우저 개발자도구나 API 직접 호출로 amount를 0원이나 1원으로 조작해 보낼 수 있다.
  // 결제 금액은 서버가 고정값으로 강제해야 하므로, 클라이언트에서는 더 이상 amount/name을 받지 않는다.
  const PLAN_AMOUNT = 3900;
  const PLAN_NAME = '비즈맵 지원사업 알림 (월간 정기구독)';

  const { customer_uid, merchant_uid, buyer_email, buyer_name } = req.body;

  if (!customer_uid || !merchant_uid) {
    return res.status(400).json({ success: false, message: '필수 파라미터가 누락되었습니다.' });
  }

  const impKey = process.env.IMP_KEY;
  const impSecret = process.env.IMP_SECRET;
  if (!impKey || !impSecret) {
    return res.status(500).json({ success: false, message: 'IMP_KEY/IMP_SECRET 환경 변수가 설정되지 않았습니다.' });
  }

  try {
    // 1. 포트원 인증 토큰 발급
    const tokenRes = await fetch('https://api.iamport.kr/users/getToken', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        imp_key: impKey,
        imp_secret: impSecret
      })
    });

    const tokenData = await tokenRes.json();
    if (tokenData.code !== 0) {
      return res.status(400).json({ success: false, message: '포트원 토큰 발급 실패: ' + tokenData.message });
    }

    const accessToken = tokenData.response.access_token;

    // 2. 등록된 카드(customer_uid)로 첫 달 3,900원 출금 승인 요청
    const payRes = await fetch('https://api.iamport.kr/subscribe/payments/again', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`
      },
      body: JSON.stringify({
        customer_uid: customer_uid,
        merchant_uid: merchant_uid,
        amount: PLAN_AMOUNT,
        name: PLAN_NAME,
        buyer_email: buyer_email,
        buyer_name: buyer_name
      })
    });

    const payData = await payRes.json();

    if (payData.code === 0 && payData.response.status === 'paid') {
      // 3. 🔥 [자동 결제 세팅] 1달 뒤 자동 결제 스케줄을 포트원에 즉시 예약 등록
      const nextDate = new Date();
      nextDate.setMonth(nextDate.getMonth() + 1); // 정확히 1달 뒤
      const scheduleAt = Math.floor(nextDate.getTime() / 1000); // 초 단위 Unix Timestamp
      const nextMerchantUid = `bizmap_auto_${Date.now()}`;

      await fetch('https://api.iamport.kr/subscribe/payments/schedule', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`
        },
        body: JSON.stringify({
          customer_uid: customer_uid,
          schedules: [
            {
              merchant_uid: nextMerchantUid,
              schedule_at: scheduleAt,
              amount: PLAN_AMOUNT,
              name: PLAN_NAME,
              buyer_email: buyer_email,
              buyer_name: buyer_name
            }
          ]
        })
      });

      return res.status(200).json({
        success: true,
        imp_uid: payData.response.imp_uid,
        merchant_uid: payData.response.merchant_uid,
        paid_amount: payData.response.amount,
        next_billing_date: nextDate.toISOString()
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
