# VSSBOT

Volkan Arslan hesabının Ticarion PC otomasyonuna katılım kararını Telegram üzerinden
7/24 yöneten bağımsız Railway servisidir. BlueStacks ve oyun otomasyonu bu repoda yoktur.

## Telegram komutları

- `/calistir`: Sıradaki PC turuna bir kez katıl; karar PC tarafından alınınca otomatik kapanır.
- `/surekli`: Her PC otomasyon turuna katıl.
- `/iptal`: Tek seferlik ve sürekli bütün çalışma izinlerini kapat.
- `/durum`: Mevcut tercihi göster.
- `/saat 04.16 08:30 14`: Global otomatik turu seçilen Türkiye saat ve dakikalarında çalıştır.
- `/saat her`: Global otomatik turu her saat başına döndür.
- `/saat kapat`: Global otomatik turları durdur.
- `/saatler`: Geçerli global saat planını göster.
- `/etkinlik [gün]`: Railway'e aktarılan etkinlik ödüllerinin toplam listesi.
- `/etkinlikhesap [gün]`: Etkinlik ödüllerinin hesap bazlı listesi.
- `/etkinlikac`, `/etkinlikkapat`, `/etkinlikdurum`: Etkinlik kodunu silmeden açar, kapatır ve durumunu gösterir.
- `/yardim`: Kullanım panelini göster.

Komutlar yalnız `VOLKAN_USERNAME` sahibinin ve `ADMIN_USERNAME` yöneticisinin özel
sohbetinden kabul edilir. Varsayılan yönetici `@JackTheRipppper` hesabıdır. İlk başarılı
eşleşmede iki hesabın Telegram kullanıcı kimliği ayrı ayrı kaydedilir ve sonraki komutlarda
kimlik doğrulanır. Her iki yetkili de hesabı açabilir, sürekli moda alabilir, kapatabilir ve
durumunu görebilir.

Global saat planını yalnız `ADMIN_USERNAME` değiştirebilir. Her iki yetkili `/saatler` ile
planı görebilir. Saatler `Europe/Istanbul` (Türkiye) saat dilimindedir.

## Railway kurulumu

1. Bu GitHub reposundan yeni ve ayrı bir Railway servisi oluştur.
2. `TELEGRAM_BOT_TOKEN`, `CONTROL_SECRET`, `VOLKAN_USERNAME=vlknarslan` ve
   `ADMIN_USERNAME=jacktheripppper` değişkenlerini ekle.
3. Kalıcı bir Railway Volume oluşturup `/data` yoluna bağla.
4. Servis için public domain üret. Sağlık kontrolü `GET /health` adresindedir.

Volume kullanılmazsa Railway yeniden dağıtımında kayıtlı mod ve Telegram offset'i kaybolabilir.

## PC karar API'si

PC, her otomatik turdan hemen önce aşağıdaki isteği gönderir:

```http
POST /api/claim
Authorization: Bearer CONTROL_SECRET
```

Örnek yanıt:

```json
{"ok":true,"active":true,"mode":"once","consumed":true,"claimed_at":"..."}
```

`once` kararı bu istek sırasında atomik olarak tüketilir. `always`, `/iptal` gelene kadar
aktif kalır. Salt okunur durum kontrolü `GET /api/status` üzerinden aynı yetkilendirmeyle
yapılabilir.

PC otomasyonu etkinlik sonuçlarını aynı kimlik doğrulamasıyla `POST /api/events`
adresine gönderir. Kayıtlar `external_id` ile tekilleştirilir.

Saat planı `GET /api/schedule` ile okunur ve `POST /api/schedule` ile güncellenir. POST
gövdesi örneği: `{"enabled":true,"times":["04:16","08:30"]}`. Boş `times` listesi her saat
anlamına gelir.

Android kontrol paneli `GET /api/dashboard`, `POST /api/mode` ve
`GET/POST /api/event-control` uçlarını kullanır. Tüm uçlar aynı Bearer yetkilendirmesini
zorunlu tutar. Etkinlik modülü ilk kurulumda kapalıdır; kod ve eski ödül kayıtları korunur.
