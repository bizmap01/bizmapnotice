// lib/emailAuth.js
// 이메일 회원가입/로그인 전용 인증 헬퍼.
// 예전엔 index.html이 브라우저에서 anon 키로 users 테이블을 직접 조회해서
// "이메일이 존재하기만 하면" 로그인 성공 처리를 했다(비밀번호 검증 자체가 없었음).
// 지금은 비밀번호를 bcrypt로 해시해서 password_hash 컬럼에 저장하고,
// 로그인 성공 시 서버(이 파일)가 서명한 토큰을 발급한다.
// 이후 마이페이지 조회/수정 등은 이 토큰을 Authorization: Bearer <token> 헤더로
// 실어 보내야만 처리되고, 서버는 이 파일의 verifySessionToken으로 토큰이
// 위조되지 않았는지 + 만료되지 않았는지를 검증한 뒤에만 해당 유저 데이터를 다룬다.
import bcrypt from 'bcryptjs';
import crypto from 'crypto';

const TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30; // 토큰 유효기간 30일

function base64url(str) {
  return Buffer.from(str, 'utf8').toString('base64url');
}

function base64urlDecode(str) {
  return Buffer.from(str, 'base64url').toString('utf8');
}

function getSecret() {
  const secret = process.env.EMAIL_AUTH_SECRET;
  if (!secret) {
    throw new Error('서버 환경변수 EMAIL_AUTH_SECRET이 설정되지 않았습니다.');
  }
  return secret;
}

/** 회원가입 시 평문 비밀번호를 해시로 변환 (DB엔 이 해시값만 저장) */
export async function hashPassword(password) {
  const salt = await bcrypt.genSalt(10);
  return bcrypt.hash(password, salt);
}

/** 로그인 시 입력한 평문 비밀번호가 저장된 해시와 일치하는지 검증 */
export async function verifyPassword(password, hash) {
  if (!hash) return false;
  return bcrypt.compare(password, hash);
}

/** 로그인 성공 시 발급하는 서명된 세션 토큰 (payload.signature 형태) */
export function issueSessionToken(email) {
  const payload = {
    email: email.toLowerCase(),
    exp: Math.floor(Date.now() / 1000) + TOKEN_TTL_SECONDS,
  };
  const payloadStr = base64url(JSON.stringify(payload));
  const signature = crypto.createHmac('sha256', getSecret()).update(payloadStr).digest('base64url');
  return `${payloadStr}.${signature}`;
}

/** 토큰의 서명과 만료시간을 검증하고, 유효하면 이메일을 반환 (아니면 null) */
export function verifySessionToken(token) {
  if (!token || typeof token !== 'string' || !token.includes('.')) return null;
  const [payloadStr, signature] = token.split('.');
  if (!payloadStr || !signature) return null;

  const expectedSignature = crypto.createHmac('sha256', getSecret()).update(payloadStr).digest('base64url');

  const sigBuf = Buffer.from(signature);
  const expectedBuf = Buffer.from(expectedSignature);
  if (sigBuf.length !== expectedBuf.length || !crypto.timingSafeEqual(sigBuf, expectedBuf)) {
    return null; // 서명 위조/변조됨
  }

  let payload;
  try {
    payload = JSON.parse(base64urlDecode(payloadStr));
  } catch {
    return null;
  }

  if (!payload.email || !payload.exp || payload.exp < Math.floor(Date.now() / 1000)) {
    return null; // 만료됨
  }

  return payload.email;
}

/** 마이페이지용 API들이 공통으로 쓰는 "로그인 필수" 체크 */
export function requireEmailUser(req) {
  const authHeader = req.headers.authorization || '';
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7).trim() : null;

  if (!token) {
    return { ok: false, status: 401, message: '로그인이 필요합니다.' };
  }

  const email = verifySessionToken(token);
  if (!email) {
    return { ok: false, status: 401, message: '세션이 만료되었거나 유효하지 않습니다. 다시 로그인해 주세요.' };
  }

  return { ok: true, email };
}
