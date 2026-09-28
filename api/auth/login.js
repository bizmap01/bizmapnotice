// api/auth/login.js
// 이메일 로그인 처리. 예전엔 users 테이블에 그 이메일 row가 "존재하기만"
// 하면 비밀번호 값과 무관하게 로그인 성공 처리를 했다(핵심 인증 버그).
// 지금은 저장된 password_hash와 입력한 비밀번호를 bcrypt로 실제 비교하고,
// 맞을 때만 로그인 유지용 토큰을 발급한다. service_role 키는 여기(서버)에서만
// 쓰이고 브라우저에는 절대 내려가지 않는다.
import { createClient } from '@supabase/supabase-js';
import { verifyPassword, issueSessionToken } from '../../lib/emailAuth.js';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ success: false, message: 'Method Not Allowed' });
  }

  const { email, password } = req.body || {};

  if (!email || !email.includes('@')) {
    return res.status(400).json({ success: false, message: '올바른 이메일 주소를 입력해 주세요.' });
  }
  if (!password) {
    return res.status(400).json({ success: false, message: '비밀번호를 입력해 주세요.' });
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

  try {
    const { data: user, error } = await supabaseAdmin
      .from('users')
      .select('email, name, password_hash')
      .eq('email', payloadEmail)
      .maybeSingle();

    if (error) {
      return res.status(500).json({ success: false, message: error.message });
    }

    // 🔒 이메일이 없는 경우와 비밀번호가 틀린 경우를 같은 메시지로 응답한다
    // (어느 쪽이 틀렸는지 구분해서 알려주면 가입 여부를 캐낼 수 있는 계정 열거 공격에 악용될 수 있음).
    const genericFail = () =>
      res.status(401).json({ success: false, message: '이메일 또는 비밀번호가 일치하지 않습니다.' });

    if (!user) {
      return genericFail();
    }

    if (!user.password_hash) {
      // 🔧 [마이그레이션] 아직 비밀번호를 설정한 적 없는 구버전 이메일 가입자.
      return res.status(409).json({
        success: false,
        migrationRequired: true,
        message: '비밀번호가 아직 설정되지 않은 계정입니다. "비밀번호 찾기"로 새 비밀번호를 먼저 설정해 주세요.',
      });
    }

    const passwordOk = await verifyPassword(password, user.password_hash);
    if (!passwordOk) {
      return genericFail();
    }

    const token = issueSessionToken(payloadEmail);
    return res.status(200).json({
      success: true,
      token,
      email: payloadEmail,
      name: user.name || payloadEmail.split('@')[0],
    });
  } catch (err) {
    return res.status(500).json({ success: false, message: err.message || '로그인 처리 중 오류가 발생했습니다.' });
  }
}
