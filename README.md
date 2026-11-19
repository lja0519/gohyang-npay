# 고향사랑e음 네이버페이 2만 원 찾기

고향사랑e음 지역이벤트 중 10만 원 기부 시 네이버페이 2만 원을 주는 행사를 매일 정리한다.
공개 API만 조회하며 로그인·기부·주문·신청은 하지 않는다.

- 결과 페이지: https://lja0519.github.io/gohyang-npay/ (매일 10:00 KST 갱신)
- 잔여 수량은 공개되지 않는다. 기부 전에 이벤트 공지를 직접 확인할 것.

## 직접 돌려 보기

파이썬 3.9 이상만 있으면 된다(추가 설치 없음).

```bash
python3 gohyang_npay.py                      # 보고서 출력
python3 gohyang_npay.py --html docs/index.html
python3 -m unittest                          # 테스트
```

## 확인 필요 행사

조건이 포스터 이미지에만 있거나 선착순·추첨 문구가 섞인 행사는 "확인 필요"로 나온다.
포스터를 보고 판정을 기록하면 공지 내용이 바뀌기 전까지 다시 묻지 않는다.

```bash
python3 gohyang_npay.py --posters            # posters/ 에 포스터 저장
python3 gohyang_npay.py --review 6036 firstcome --quota 500 --note "포스터로 확인"
```

판정값: `firstcome`(선착순) `all`(전원) `conditional`(조건부) `draw`(추첨) `other`(대상 아님) `closed`(종료)

## 게시

GitHub Actions(`.github/workflows/daily.yml`)가 매일 `./publish.sh` 를 실행한다.
보고서를 다시 만들고 바뀐 내용을 커밋·push 하며, `docs/` 가 Pages 로 게시된다.
판정을 기록한 뒤에는 `overrides.json` 을 커밋·push 해야 다음 실행에 반영된다.
