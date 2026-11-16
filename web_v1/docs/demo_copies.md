# Demo copies of the factory databases

The web app can show plans going back into the factory system. It writes **only** to
copies of the three load databases, named `<original>_demo`. The originals stay exactly as
they are, because they hold the paper's data.

You run these steps yourself, once, as the MySQL `root` user. Claude never handles
the passwords.

| Original (never written) | Copy (the app may write) |
|---|---|
| `garment_fyp_db_seed_v2_data2_load30_20261018_083214` | `garment_fyp_db_seed_v2_data2_load30_20261018_083214_demo` |
| `garment_fyp_db_seed_v2_data2_load60_20261018_091435` | `garment_fyp_db_seed_v2_data2_load60_20261018_091435_demo` |
| `garment_fyp_db_seed_v2_data2_load90_20261018_083218` | `garment_fyp_db_seed_v2_data2_load90_20261018_083218_demo` |

## 1. Make the copies

Open a terminal in the MySQL `bin` folder, for example
`C:\Program Files\MySQL\MySQL Server 8.0\bin`. Run each line and type the root password
when asked:

```
mysql -u root -p -e "CREATE DATABASE garment_fyp_db_seed_v2_data2_load30_20261018_083214_demo"
mysqldump -u root -p --single-transaction garment_fyp_db_seed_v2_data2_load30_20261018_083214 | mysql -u root -p garment_fyp_db_seed_v2_data2_load30_20261018_083214_demo

mysql -u root -p -e "CREATE DATABASE garment_fyp_db_seed_v2_data2_load60_20261018_091435_demo"
mysqldump -u root -p --single-transaction garment_fyp_db_seed_v2_data2_load60_20261018_091435 | mysql -u root -p garment_fyp_db_seed_v2_data2_load60_20261018_091435_demo

mysql -u root -p -e "CREATE DATABASE garment_fyp_db_seed_v2_data2_load90_20261018_083218_demo"
mysqldump -u root -p --single-transaction garment_fyp_db_seed_v2_data2_load90_20261018_083218 | mysql -u root -p garment_fyp_db_seed_v2_data2_load90_20261018_083218_demo
```

## 2. Let the existing read-only account read the copies

```sql
GRANT SELECT ON `garment_fyp_db_seed_v2_data2_load30_20261018_083214_demo`.* TO 'craft_reader'@'localhost';
GRANT SELECT ON `garment_fyp_db_seed_v2_data2_load60_20261018_091435_demo`.* TO 'craft_reader'@'localhost';
GRANT SELECT ON `garment_fyp_db_seed_v2_data2_load90_20261018_083218_demo`.* TO 'craft_reader'@'localhost';
```

## 3. Create the writer account (copies only)

Choose your own password in place of `CHOOSE_A_PASSWORD`. This account gets rights on the
three copies only and **no rights on any original**.

```sql
CREATE USER 'craft_writer'@'localhost' IDENTIFIED BY 'CHOOSE_A_PASSWORD';
GRANT ALL PRIVILEGES ON `garment_fyp_db_seed_v2_data2_load30_20261018_083214_demo`.* TO 'craft_writer'@'localhost';
GRANT ALL PRIVILEGES ON `garment_fyp_db_seed_v2_data2_load60_20261018_091435_demo`.* TO 'craft_writer'@'localhost';
GRANT ALL PRIVILEGES ON `garment_fyp_db_seed_v2_data2_load90_20261018_083218_demo`.* TO 'craft_writer'@'localhost';
FLUSH PRIVILEGES;
```

## 4. Tell the app

Add these lines to `web_v1/backend/.env`, the same file as the Groq key. It's gitignored
and never committed.

```
CRAFT_USE_DEMO_COPIES=1
CRAFT_WRITER_USER=craft_writer
CRAFT_WRITER_PASSWORD=CHOOSE_A_PASSWORD
```

`CRAFT_USE_DEMO_COPIES=1` makes the app read the copies. Writing only starts once
write-back is switched on too (`CRAFT_WRITEBACK=1`, added in a later step).

## Starting again

To throw away the app's changes, drop a copy and repeat its two lines from step 1. The
app's "Reset demo data" action does the same thing.
