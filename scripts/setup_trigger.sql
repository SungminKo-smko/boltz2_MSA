-- Supabase SQL Editor에서 실행할 것
-- Dashboard > SQL Editor > New query

-- 1. 프로필 자동 생성 함수
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO public.profiles (id, email, display_name, is_approved, auto_approved, created_at)
  VALUES (
    NEW.id::text,
    NEW.email,
    NEW.raw_user_meta_data->>'full_name',
    CASE WHEN NEW.email LIKE '%@shaperon.com' THEN true ELSE false END,
    CASE WHEN NEW.email LIKE '%@shaperon.com' THEN true ELSE false END,
    NOW()
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 2. 트리거 생성
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
