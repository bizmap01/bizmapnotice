// api/webhook.js
import { createClient } from '@supabase/supabase-js';

// 🔒 [보안수정] anon(publishable) 키 대신 service_role 키를 쓴다. 이 웹훅은 서버(Vercel)에서만
// 실행되므로 RLS를 우회해서 users 테이블을 갱신할 권한이 필요하고, RLS를 제대로 켠 뒤에는
// anon 키로는 어차피 이 update가 통과되지 않는다.
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ message: 'Method Not Allowed' });
  }

  // 🔒 (선택) 포트원 콘솔에 등록한 웹훅 URL에 ?secret=... 쿼리파라미터를 붙여뒀다면,
  // 그 값을 여기서 검증해서 아무나 이 엔드포인트를 호출해 가짜 결제완료를 위조하지 못하게 한다.
  // PORTONE_WEBHOOK_SECRET 환경변수를 설정하지 않으면 이 검증은 건너뛴다(기존과 동일한 수준).
  const webhookSecret = process.env.PORTONE_WEBHOOK_SECRET;
  if (webhookSecret && req.query.secret !== webhookSecret) {
    return res.status(401).json({ success: false, message: '인증되지 않은 웹훅 요청입니다.' });
  }

  const { imp_uid, merchant_uid, status } = req.body;

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
