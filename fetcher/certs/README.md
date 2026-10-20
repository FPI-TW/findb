# RDS CA bundle

`ap-southeast-1-bundle.pem` 是公開的 Amazon RDS 區域 CA bundle，
下載來源：https://truststore.pki.rds.amazonaws.com/ap-southeast-1/ap-southeast-1-bundle.pem 。

FactSet image 使用此檔驗證來源 RDS 的 CA chain 與 hostname。這不是 client credential，
不能關閉 hostname verification 來繞過輪替問題。更新時從官方來源重新取得 bundle，
重建 image 並執行 TLS smoke check；不得將 repo 的 private connection JSON 包入 image。
