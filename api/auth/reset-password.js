// api/auth/reset-password.js
// 이메일 로그인 계정의 비밀번호 재설정. 예전엔 index.html이 브라우저에서
// anon 키로 users 테이블을 직접 조회해서 본인확인(휴대폰 번호가 keywords
// 문자열 안에 들어있는지)까지만 하고, 실제로는 DB를 전혀 바꾸지 않은 채
// "변경 완료" 알럿만 띄웠다 — 그래서 password_hash가 없는 구버전 가입자는
// 이 화면을 거쳐도 영원히 로그인이 안 됐다. 지금은 서버(이 파일)가
// service_role 키로 본인확인을 하고, 새 비밀번호를 bcrypt로 해시해서
// 실제로 password_hash 컬럼을 갱신한다.
import { createClient } from '@supabase/supabase-js';
import { hashPassword, issueSessionToken } from '../../lib/emailAuth.js';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ success: false, message: 'Method Not Allowed' });
  }

  const { email, phone, newPassword } = req.body || {};

  if (!email || !email.includes('@')) {
    return res.status(400).json({ success: false, message: '올바른 이메일 주소를 입력해 주세요.' });
  }
  if (!phone || !phone.trim()) {
    return res.status(400).json({ success: false, message: '휴대폰 번호를 입력해 주세요.' });
  }
  if (!newPassword || newPassword.length < 6) {
    return res.status(400).json({ success: false, message: '비밀번호는 최소 6자리 이상이어야 합니다.' });
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceRoleKey) {
    return res.status(500).json({ success: false, message: '서버 환경변수(SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY)가 설정되지 않았습니다.' });
  }

  const payloadEmail = email.trim().toLowerCase();
  const supabaseAdmin = createClient(supabaseUrl, serviceRoleKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });

  // 🔒 이메일이 없는 경우와 휴대폰 번호가 안 맞는 경우를 같은 메시지로 응답한다
  // (login.js와 동일하게, 어느 쪽이 틀렸는지 알려주면 계정 열거 공격에 악용될 수 있음).
  const genericFail = () =>
    res.status(401).json({ success: false, message: '입력하신 이메일 또는 휴대폰 번호와 일치하는 회원 정보를 찾을 수 없습니다.' });

  try {
    const { data: user, error } = await supabaseAdmin
      .from('users')
      .select('email, keywords')
      .eq('email', payloadEmail)
      .maybeSingle();

    if (error) {
      return res.status(500).json({ success: false, message: error.message });
    }
    if (!user) {
      return genericFail();
    }

    // ⚠️ 기존 index.html 로직과 동일하게, 휴대폰 번호는 별도 컬럼이 아니라
    // keywords 문자열 안에 포함돼 있는지로 본인확인한다(스키마 변경 없이 기존 동작 유지).
    const kwStr = Array.isArray(user.keywords) ? user.keywords.join(' ') : (user.keywords || '');
    if (!kwStr.includes(phone.trim())) {
      return genericFail();
    }

    const password_hash = await hashPassword(newPassword);
    const { error: updateError } = await supabaseAdmin
      .from('users')
      .update({ password_hash })
      .eq('email', payloadEmail);

    if (updateError) {
      return res.status(500).json({ success: false, message: updateError.message });
    }

    const token = issueSessionToken(payloadEmail);
    return res.status(200).json({ success: true, token, email: payloadEmail });
  } catch (err) {
    return res.status(500).json({ success: false, message: err.message || '비밀번호 재설정 처리 중 오류가 발생했습니다.' });
  }
}
