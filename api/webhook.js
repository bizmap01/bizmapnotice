// api/webhook.js
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = 'https://hcyvfgeaquydsvtrcnrv.supabase.co';
const SUPABASE_KEY = 'sb_publishable_P19tdkj74ibIy7Xdle2i4w_M1B1mhV_';
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ message: 'Method Not Allowed' });
  }

  const { imp_uid, merchant_uid, status } = req.body;

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
      return res.status(400).json({ success: false, message: '포트원 토큰 발급 실패' });
    }
    const accessToken = tokenData.response.access_token;

    // 2. 포트원 결제 상세 내역 단건 조회 (실제 결제 승인 여부 검증)
    const paymentRes = await fetch(`https://api.iamport.kr/payments/${imp_uid}`, {
      method: 'GET',
      headers: { 'Authorization': `Bearer ${accessToken}` }
    });
    const paymentData = await paymentRes.json();
    if (paymentData.code !== 0) {
      return res.status(400).json({ success: false, message: '결제 정보 조회 실패' });
    }

    const payment = paymentData.response;

    // 3. 결제 성공(paid) 상태인 경우에만 다음 달 결제 자동 예약 등록
    if (payment.status === 'paid') {
      const customerUid = payment.customer_uid;
      const buyerEmail = payment.buyer_email;

      // 만약 해지 예약자(cancel_reserved)이거나 이미 차단된 유저면 다음 달 예약을 걸지 않음
      if (buyerEmail) {
        const { data: user } = await supabase
          .from('users')
          .select('subscription_status')
          .eq('email', buyerEmail)
          .maybeSingle();

        if (user && user.subscription_status === 'cancel_reserved') {
          return res.status(200).json({ success: true, message: '해지 예약 회원으로 다음 결제 예약 생략' });
        }
      }

      // 정확히 1달 뒤 시점(초 단위 정수) 계산
      const nextDate = new Date();
      nextDate.setMonth(nextDate.getMonth() + 1);
      const scheduleAtSeconds = Math.floor(nextDate.getTime() / 1000);

      const nextMerchantUid = `bizmap_auto_${Date.now()}`;

      // 포트원에 다음 달 3,900원 결제 스케줄 재등록
      const scheduleRes = await fetch('https://api.iamport.kr/subscribe/payments/schedule', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`
        },
        body: JSON.stringify({
          customer_uid: customerUid,
          schedules: [
            {
              merchant_uid: nextMerchantUid,
              schedule_at: scheduleAtSeconds,
              amount: payment.amount,
              name: payment.name,
              buyer_email: payment.buyer_email,
              buyer_name: payment.buyer_name
            }
          ]
        })
      });

      // DB 만료일 및 결제 이력 갱신
      if (buyerEmail) {
        await supabase
          .from('users')
          .update({
            subscription_status: 'active',
            next_billing_date: nextDate.toISOString(),
            last_imp_uid: imp_uid,
            last_merchant_uid: merchant_uid,
            updated_at: new Date().toISOString()
          })
          .eq('email', buyerEmail);
      }

      return res.status(200).json({ success: true, message: '결제 확인 및 익월 예약 자동 갱신 완료' });
    }

    return res.status(200).json({ success: true, message: '처리할 결제 상태 아님' });
  } catch (err) {
    console.error('웹훅 처리 에러:', err);
    return res.status(500).json({ success: false, message: '서버 에러: ' + err.message });
  }
}
