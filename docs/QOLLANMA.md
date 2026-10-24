# Yordamchi — qo'llanma (dev bot, P1)

Telefonda o'qish uchun. Qo'shtirnoq ichidagi bot javoblari — botning aynan o'zi yozadigan matn.
`{loyiha}`, `{vaqt}` kabi joylar o'rniga haqiqiy qiymat chiqadi.

---

## 1. Boshlash

- **Dev bot** — `@OrvionXBot`. U faqat **"OrvionX Projects"** yopiq guruhida ishlaydi.
- **Shaxsiy bot** — `@OrvionX_AI_Bot`. U keyinroq, P3 bosqichida ishga tushadi.
- Botlardan **faqat siz** foydalana olasiz. Boshqa odamning xabari e'tiborsiz qoladi.
- Guruhda **anonim admin** sifatida yozmang: unda xabar sizdan emas, `GroupAnonymousBot`dan
  kelgandek ko'rinadi va bot uni tashlab yuboradi.
- Mac doim **zaryadkada va uyg'oq** tursin. Mac uxlasa, bot ham javob bermaydi.

<!-- SCREENSHOT: "OrvionX Projects" guruhi, tepada qadalgan "Onlayn: HH:MM" xabari -->

---

## 2. Qanday yozish kerak

- Loyiha nomini ayting ("BonviHR'da login sahifasini tuzat") yoki ishni tasvirlab bering —
  bot loyihani o'zi topadi. Reply qilish shart emas.
- Bot qaysi loyihaga yuborganini birinchi qatorda aytadi:
  - "→ {loyiha}"
  - "→ {loyiha} (ishlayotgan topshiriqqa qo'shdim)" — ish allaqachon ketayotgan bo'lsa.
- Ikkilansa, bitta savol beradi: "«…» — BonviHR'gami yoki HRMentor'gami?"
  - Javob: `2`, `ikkinchisi`, loyiha nomi yoki `hech biri`.
  - 30 daqiqada javob bo'lmasa, xabar hech qayerga yuborilmaydi.
- **Skrinshot + matn + ovoz** ketma-ket yuborilsa, bitta so'rov hisoblanadi.
  - Rasmdan keyin bot taxminan 10 soniya kutadi; matndan keyin 1–2 soniya.
  - Bir so'rov ko'pi bilan 30 soniya yig'iladi.
- **"kut"** (yoki "kutib tur", "biroz kut") — "hali yana yuboraman" degani, hech qachon "to'xta" emas:
  - bot keyingi rasm, matn yoki ovozni 2 daqiqagacha kutadi va ularni bitta so'rovga qo'shadi;
  - bot "kut"ga 👌 reaksiya qo'yadi — kutayotganini shundan bilasiz;
  - ishlayotgan ish **to'xtamaydi**; 2 daqiqada hech narsa kelmasa, "kut" shunchaki unutiladi.
  - To'xtatish uchun faqat `to'xta`, `stop`, `bas` (pastdagi jadval).
- **"Yozmoqda…"** faqat tez javoblarda (hisobot, `holat?`, sozlamalar) javob kelguncha ko'rinadi; loyiha ishida
  👀 reaksiya va jonli qator yetarli. Ish paytida ham ko'rsin desangiz: `sozlama: ack.typing_for_work true`.
- **Ovozli xabar** o'zbek, rus va inglizcha aralash bo'lsa ham tushuniladi. Bot uni matnga
  o'girib, aytganingizni qaytaradi: "Ovoz: «…» → {loyiha}".
- Ovozni tushunmasa: "Ovozli xabarni matnga o'gira olmadim: … Matn bilan yozing yoki qayta yuboring."
- **Atamani o'rgatish**: `bu so'zni to'g'ri yoz: OrvionX`
  - Javob: "Endi «OrvionX» so'zini aynan shunday yozaman."
  - Keyingi ovozli xabardan boshlab ishlaydi.

---

## 3. Loyiha ishlari

- Ko'rsatma, qo'shimcha va tuzatishlarni oddiy gap bilan yozing. Ish ketayotganda yozgan
  qo'shimchangiz o'sha ishga qo'shiladi.
- Ish paytida bitta "jonli" qator yangilanib turadi: "{loyiha} · ishlayapti 4 daq · 12 qadam".
- Oxirida natija alohida xabar bo'lib keladi: Natija, Fayllar, Testlar, Commit, Vaqt.
- **Commit va push avtomatik.** Natijada ko'rinadi:
  - "Commit: {sha} · push: bajarildi ({upstream})"
  - "Commit: commit/push qilinmadi" — agent o'zi qilmasa.

**To'xtatish va yo'nalishni o'zgartirish**

| Nima yozasiz | Nima bo'ladi |
|---|---|
| `to'xta` (`stop`, `bas`) | Ishlayotgan ish to'xtaydi: "{loyiha}: to'xtatdim." |
| `BonviHR'ni to'xtat` yoki `/stop BonviHR` | Aynan shu loyiha to'xtaydi |
| `yo'q, boshqacha qil: <yangi ko'rsatma>` | Joriy ish to'xtaydi, yangi ko'rsatma bilan davom etadi |
| `hammasini to'xtat` | Hamma ishlar to'xtaydi |
| `BonviHR yangi sessiya` | "BonviHR: eski sessiyani yopdim — keyingi topshiriq yangi sessiyada, oxirgi eslatma bilan." |

- Bir nechta ish ketayotgan bo'lsa va loyiha aytilmasa: "Bir nechta ish ketyapti: … Qaysi birini
  to'xtatay? Loyiha nomini yozing." Loyiha nomini yozing yoki uning xabariga reply qiling.
- Hech narsa ishlamayotgan bo'lsa: "Hozir ishlayotgan topshiriq yo'q."
- Boshqa odamdan **forward qilingan** "to'xta" hech narsani to'xtatmaydi.
- **Yangi sessiya**: agent chalkashib qolsa, `BonviHR yangi sessiya` deng. Keyingi topshiriq yangi
  sessiyada, oxirgi ish eslatmasi bilan boshlanadi. Ish ketayotgan bo'lsa, u eski sessiyada tugaydi:
  "BonviHR: hozirgi ish eski sessiyada tugaydi; keyingi topshiriq yangi sessiyada, oxirgi eslatma bilan."

**Noto'g'ri loyihaga ketdi**

1. Yozing: `bu HRMentor uchun edi` (oxirgi 2 soat ichidagi ish uchun).
2. Bot: "→ HRMentor: topshiriqni BonviHR'dan olib, HRMentor'ga qayta yubordim."
3. Bot o'zgargan fayllarni sanab so'raydi: "… Ularni avvalgi holatiga qaytaraymi? (ha / yo'q)"
4. `ha` — qaytaradi, `yo'q` — o'z holicha qoldiradi.

**Loyihalar ro'yxati**

- `loyihalar` yoki `/loyihalar` — hamma loyihalar.
- `BonviHR'ni «bonvi» deb tani` — qisqa nom qo'shish.
- `BonviHR tavsifi: kadrlar tizimi` — tavsif yozish.
- `BonviHR'ni arxivla` / `BonviHR'ni arxivdan chiqar`.

<!-- SCREENSHOT: ishlayotgan topshiriqning jonli qatori va natija xabari -->

---

## 4. Savollar va qarorlar

**Agent savoli** shunday keladi: "{loyiha} · Savol: …" va pastida raqamlangan variantlar.

Javob berish usullari (hammasi ishlaydi):

- raqam: `2`, `2-chi`, `ikkinchisi`;
- variant matni: `PostgreSQL` (yoki `postgres`);
- o'z so'zlaringiz;
- ovozli xabar;
- savolga reply.

Bir nechta savol bo'lsa — har biriga alohida qatorda javob yozing. Ko'p tanlovli savolda: `1, 3`.

- **Ikki loyihada savol ochiq bo'lsa**, loyihani ayting: `BonviHR 2`.
  - Aytmasangiz, bot so'raydi: "«2» — BonviHR savoligami yoki HRMentor savoligami?"
- Javob qabul qilinsa: "→ {loyiha}: javob «…»".

**Muhim qarorlar** (deploy, prod, serverda o'zgarish, ma'lumot o'chishi, pul sarfi, force push):

- Bot har bir qaror uchun **bitta** savol beradi:
  "{loyiha} · Muhim qaror #17: … Oqibati: … Bajaraymi? (ha / yo'q / boshqa ko'rsatma)"
- `ha` — "{loyiha} · #17 tasdiqlandi — aynan shu buyruq bir marta bajariladi."
- `yo'q` — "{loyiha} · #17 bekor qilindi, bajarilmaydi."
- Boshqa ko'rsatma yozsangiz — buyruq bajarilmaydi, ko'rsatmangiz agentga boradi.
- Bir nechta qaror ochiq bo'lsa: savolga reply qiling yoki raqamini yozing (`#17 ha`).

**Forward qilingan xabar** hech qachon sizning javobingiz hisoblanmaydi — u faqat material.

<!-- SCREENSHOT: "Muhim qaror #…" savoli va "ha" javobi -->

---

## 5. Hisobot va holat

| Nima yozasiz | Nima olasiz |
|---|---|
| `BonviHR qanday?` yoki `/hisobot BonviHR` | Bitta loyiha: hozirgi ish, oxirgi natija, git, bugungi commit'lar |
| `bugun nima o'zgardi?` yoki `/hisobot` | Bugun o'zgargan hamma loyihalar |
| `holat?` yoki `/holat` | Botning o'zi: Claude CLI, ClaudeManager, ishlar, navbat, Mac xotirasi va diski, oxirgi xatolar |
| `xarajat?` yoki `/xarajat` | Bugun / 7 kun / shu oy: pullik API (limitga nisbatan), Claude obunasi (taxmin), eng faol loyihalar |

- Har hisobot oxirida ma'lumot qanchalik yangiligi yoziladi, masalan:
  - "Ma'lumot: 12 daqiqa oldin"
  - "Ma'lumot: eskirgan edi (3 soat oldin) — hozir yangiladim"
- Hisobot loyiha sessiyasiga tegmaydi va ishlayotgan ishni to'xtatmaydi.
- **Chuqur savol** ("BonviHR'da login nega ishlamayapti?", "X'dagi xatoni tekshir") — bot darhol
  (2 soniya ichida) yozadi: "{loyiha} · Tekshiryapman (~3 daqiqa)", keyin natijani alohida yuboradi.
  Tekshiruv faqat o'qiydi, hech narsani o'zgartirmaydi.
- **Xarajat limiti**: pullik xizmat oylik limitining 80 % iga yetsa ogohlantiradi; 100 % da uning pullik
  chaqiruvlarini to'xtatadi va limitni qanday oshirishni aytadi.

---

## 6. Serverlar

| Nima yozasiz | Nima bo'ladi |
|---|---|
| `bonvi-srv serverini kuzat` | "bonvi-srv: kuzatuvga oldim — har … daqiqada faqat o'qiydigan buyruqlar bilan tekshiraman." |
| `bonvi-srv'ni kuzatma` | "bonvi-srv: kuzatuvdan chiqardim." |
| `serverda nima bo'lyapti?` yoki `bonvi-srv qanday?` | Har server uchun bitta qator, oxirida "Ma'lumot: …" |

- Server nomi — `~/.ssh/config` dagi alias (bitta so'z).
- Yangi tekshiruv kerak bo'lsa, avval "Tekshiryapman (~… daqiqa): …" keladi.
- Muammo topilsa, bot o'zi yozadi: "bonvi-srv serverida muammo: …" (har muammo bir marta).
- Faqat o'tish (jump) uchun belgilangan serverda bot hech qanday buyruq ishlatmaydi.

---

## 7. Sozlamalar

- **Ko'rish**: `sozlamalaring qanday?` (yoki `/sozlamalar`). Qisqa ro'yxat + to'liq ro'yxat faylda.
- **O'zgartirish**: gapni `sozlama:` bilan boshlang (`sozlama: rasmdan keyin 15 soniya kut`) yoki botga
  "sen" deb, o'zi haqida ayting — loyiha nomisiz: `javoblaringni qisqaroq qil`, `ovozingni ayolga o'zgartir`,
  `gaplaringni iliqroq qil`. Bunday so'z bo'lmasa (`rasmdan keyin 15 soniya kut`), `sozlama:` qo'shing.
  - Javob: "Sozlama o'zgardi: … Keyingi xabardan kuchga kiradi."
  - Tushunmasa, bitta aniqlashtiruvchi savol beradi.
  - Loyiha nomi bo'lsa, bu loyiha ishi: `BonviHR'ning sozlamalar sahifasini tuzat` BonviHR'ga ketadi.
  - Sozlama bilan bo'lmaydigan narsa (yangi imkoniyat) uchun bot P2 bosqichidan keyin
    `o'zingga … qo'sh` deyishni taklif qiladi.
- **Qaytarish**: `oldingi holatiga qaytar` (oxirgi o'zgarish) yoki
  `oldingi holatiga qaytar burst.after_media_s` (bitta sozlama).
- **Himoyani susaytiradigan** o'zgarish bitta savol beradi:
  "Sozlamalar · Muhim qaror #…: … Bu himoyani susaytiradi. Qo'llaymi? (ha / yo'q)"

---

## 8. Kalitlar

- Gemini (`AIza…`) yoki OpenRouter (`sk-or-v1-…`) kalitini guruhga oddiy xabar qilib tashlang.
- Bot:
  1. xabaringizni o'chiradi;
  2. kalitni shifrlab saqlaydi: "Kalit shifrlab saqlandi: …";
  3. ovoz holatini aytadi, masalan: "Ovoz: OpenRouter (Gemini) ulandi."
- Xabarni o'chira olmasa: "Xabaringizni o'chira olmadim … Kalit chatda qoldi — kalitni
  almashtiring." Bunda kalitni albatta yangisiga almashtiring.
- Claude login tokenini bot qabul qilmaydi — akkauntlarni ClaudeManager boshqaradi.
- Kalitlar hech qachon ochiq matnli faylda saqlanmaydi va log'ga yozilmaydi.

---

## 9. Agar nimadir bo'lsa

**Qadalgan "Onlayn: HH:MM" xabari** — bot tirikligini ko'rsatadi. U har 5 daqiqada yangilanadi.
Vaqt 10 daqiqadan eski bo'lsa — bot ishlamayapti.

**Bot o'zi yozadigan xabarlar**

| Xabar | Ma'nosi |
|---|---|
| "Mac uxlagan edi 02:10–07:45, 3 ta xabar qayta ishlanmoqda" | Mac uxlagan; yozganlaringiz yo'qolmagan |
| "Internet uzilgan edi 14:02–14:20" | Tarmoq uzilgan edi, endi tiklandi |
| "Qayta ishga tushdim (…). Uzilib qolgan ishlar: … — davom ettiraymi?" | Bot qayta yondi; `davom ettir` yoki `BonviHR'ni davom ettir` deb yozing |
| "Qayta ishga tushdim (…). Uzilib qolgan ish yo'q." | Hammasi joyida |
| "{loyiha}: Claude limiti tugadi — … o'zim davom ettiraman." | Hech narsa qilish shart emas |
| "{loyiha}: … — to'xtatib turibman. Davom ettiraymi?" | Ish juda uzoq yoki bir xil xato takrorlandi; `davom ettir` yoki `to'xta` |
| "{loyiha}: oxirgi topshiriq bajarilmay qoldi — qayta yuboraymi?" | `qayta yubor` deb yozing |

**Bot jim bo'lsa**

1. Qadalgan "Onlayn: …" vaqtiga qarang.
2. `holat?` deb yozing.
3. Javob bo'lmasa — Mac'ni tekshiring: yoqiqmi, zaryadkadami, uyg'oqmi, internet bormi.
4. Mac'da hammasi joyida bo'lsa — bot jarayonini `docs/DEPLOY.md` §7 ("How the owner checks
   each thing") bo'yicha tekshiring; qayta ishga tushirish — §4.3.
5. Bot qayta yonganda yozganlaringizni o'zi qayta ishlaydi — qayta yuborish shart emas.

---

## 10. Hali yo'q

Quyidagilarga bot "Bu imkoniyat hali yo'q — P2 bosqichida qo'shiladi." deb javob beradi va
hech qaysi loyihaga yubormaydi:

- integratsiyalar: `integratsiyalar`, `/integratsiyalar`, `X'ga ulan: <manzil>`;
- yangi loyiha: `yangi loyiha: …` (shuningdek alohida `yangi loyiha`, `boshla`, `tamom` so'zlari);
- sessiyani Mac'ga o'tkazish: `/sessiya`, `BonviHR sessiyasi`;
- loyihani yopib turish va qaytarish: `BonviHR'ni yopib tur`, `BonviHR'ni davom ettiramiz`;
- botni o'zini yangilash: `o'zingga … qo'sh`.

**Shaxsiy bot** `@OrvionX_AI_Bot` va uning imkoniyatlari — P3 bosqichida.
