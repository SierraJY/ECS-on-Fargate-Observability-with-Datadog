### 1 개요

- Goal
    - 좌석 예약 도메인의 MSA를 ECS Fargate + Service Connect로 구축하고, Datadog으로 인프라·로그·APM(및 네트워크, 검증 후 반영 예정)을 통합 관측

---

### 2 서비스 구성

#### 2.1 토폴로지

- `Gateway` → `Reservation` →`{ Inventory, Payment } (병렬)` → `Notification`

#### 2.2 서비스별 상세

- **Gateway**
    - 역할: 외부 요청 진입점, Reservation으로 전달
    - 엔드포인트: `POST /reservations`, `GET /health`
- **Reservation**
    - 역할: Inventory(좌석 잠금)와 Payment(결제)를 병렬 호출. 둘 다 성공 시 Inventory 확정 처리 + Notification 호출. 하나라도 실패 시 Inventory 잠금 해제(보상 트랜잭션)
    - 엔드포인트: `POST /reservations`, `GET /health`
    - 호출 대상: Inventory, Payment, Notification (Service Connect FQDN, `<discoveryName>.jy-project`으로 접근 — 3.4.1 참고)
- **Inventory**
    - 역할: 좌석 재고 상태 관리, 동시 예약 충돌 방지, 유일하게 RDS 사용
    - DB: RDS PostgreSQL, `seats` 테이블 — `seat_id`, `status`(AVAILABLE/LOCKED/BOOKED), `locked_by`, `locked_at`
    - 엔드포인트:
        - `POST /seats/{seat_id}/lock` — AVAILABLE일 때만 LOCKED 전환(낙관적 락), 이미 잠긴 경우 409
        - `POST /seats/{seat_id}/confirm` — LOCKED → BOOKED
        - `POST /seats/{seat_id}/release` — LOCKED → AVAILABLE (결제 실패 시 롤백)
        - `GET /seats` — 전체 좌석 상태 조회
        - `GET /health`
- **Payment**
    - 역할: 결제 처리 mock, 외부 PG 호출 지연을 흉내냄
    - 엔드포인트: `POST /charge`, `GET /health`, `POST /admin/chaos` (장애 주입)
- **Notification**
    - 역할: 예약 확정 알림 mock (실제 발송 없이 로그만 남김)
    - 엔드포인트: `POST /notify`, `GET /health`

#### 2.3 공통 사항

- Fast API로 구축예정
- `portMappings`에 `appProtocol: HTTP` 지정 (Service Connect L7 메트릭 활성화 조건)
- 5개 서비스 모두 Datadog Agent 사이드카 + FireLens 사이드카 + dd-trace 계측 동일 적용

#### 2.4 API 상세 및 동작 흐름 (Phase 1 구현 기준)

- 전체 목적: 좌석을 예약하면 재고 확정(Inventory)과 결제(Payment)가 동시에 처리되고, 하나라도 실패하면 잠갔던 좌석을 다시 풀어주는(보상 트랜잭션) 흐름을 검증하는 좌석 예약 MSA 데모
- 외부에서 접근 가능한 진입점은 ALB → `Gateway` 하나뿐. `Reservation`/`Inventory`/`Payment`/`Notification`은 ALB에 연결되지 않고 Service Connect를 통한 내부 통신만 가능(3.2 ALB 참고)

**정상 흐름 (좌석 예약 성공)**

1. 클라이언트가 `Gateway`의 `POST /reservations`(body `{seat_id, user_id}`) 호출
2. `Gateway`는 받은 요청을 그대로 `Reservation`의 `POST /reservations`로 전달(pass-through, 응답도 그대로 반환)
3. `Reservation`은 `Inventory`의 `POST /seats/{seat_id}/lock`과 `Payment`의 `POST /charge`를 **동시에(병렬, asyncio.gather)** 호출
4. 둘 다 200이면 `Inventory`의 `POST /seats/{seat_id}/confirm` 호출 → 좌석 상태 `LOCKED` → `BOOKED`로 전환
5. confirm 성공 시 `Notification`의 `POST /notify` 호출(실제 발송 없이 로그만 남김)
6. `Gateway`가 최종 결과를 그대로 클라이언트에 반환: `{"status": "booked", "seat_id": ..., "user_id": ...}` (200)

**실패 흐름 (보상 트랜잭션)**

- `Inventory` lock 또는 `Payment` charge 중 하나라도 실패하면, 성공했던 쪽(주로 lock)을 `Inventory`의 `POST /seats/{seat_id}/release`로 되돌려 좌석을 `AVAILABLE` 상태로 복구시킨 뒤 409 반환 — `{"detail": "reservation failed: inventory failed"}` 또는 `payment failed`
- lock·charge는 둘 다 성공했으나 confirm이 실패하는 예외 케이스는 마찬가지로 release 호출 후 502 반환

**서비스별 API 상세**

| 서비스 | Method / Path | Request Body | 성공 응답 | 실패 조건 |
|---|---|---|---|---|
| Gateway | `POST /reservations` | `{seat_id, user_id}` | Reservation 응답을 상태 코드 포함 그대로 전달 | Reservation 응답 그대로 전달 |
| Gateway | `GET /health` | - | `{"status":"ok"}` | - |
| Reservation | `POST /reservations` | `{seat_id, user_id}` | `{"status":"booked", seat_id, user_id}` (200) | Inventory/Payment 실패 시 409, confirm 실패 시 502 |
| Reservation | `GET /health` | - | `{"status":"ok"}` | - |
| Inventory | `POST /seats/{seat_id}/lock` | `{locked_by}` | `{seat_id, status:"LOCKED"}` (200) | 이미 잠김/BOOKED이거나 존재하지 않는 좌석 → 409 |
| Inventory | `POST /seats/{seat_id}/confirm` | - | `{seat_id, status:"BOOKED"}` (200) | LOCKED 상태가 아니면 409 |
| Inventory | `POST /seats/{seat_id}/release` | - | `{seat_id, status:"AVAILABLE"}` (200) | LOCKED 상태가 아니면 409 |
| Inventory | `GET /seats` | - | 전체 좌석 배열 `[{seat_id, status, locked_by, locked_at}, ...]` | - |
| Inventory | `GET /health` | - | `{"status":"ok"}` (DB 연결 확인 포함, 실패 시 503) | - |
| Payment | `POST /charge` | `{user_id, amount}` | `{"status":"charged", user_id, amount}` (200, mock — 항상 성공. 장애 주입은 Phase 2에서 `/admin/chaos` 추가 예정) | - |
| Payment | `GET /health` | - | `{"status":"ok"}` | - |
| Notification | `POST /notify` | `{user_id, message}` | `{"status":"sent", user_id}` (200, 로그만 남기고 실제 발송 없음) | - |
| Notification | `GET /health` | - | `{"status":"ok"}` | - |

- `Inventory`는 앱 기동 시 `seats` 테이블이 없으면 자동 생성하고, 비어 있으면 `A1`~`A20` 20개 좌석을 `AVAILABLE` 상태로 시드

---

### 3. 인프라 구성

#### 3.1 VPC & 네트워크

- [VPC]
    - `jy-project-vpc(vpc-09a226651277446d9)`
    - CIDR: 10.0.0.0/24
- [Subnet]
    - Public
        - `jy-project-vpc-public-subnet-2a`
            - 10.0.0.0/26
            - ALB, NAT Gateway
        - `jy-project-vpc-public-subnet-2c`
            - 10.0.0.64/26
            - ALB
    - Private
        - `jy-project-vpc-private-subnet-2a`
            - 10.0.0.128/26
            - ECS Task, RDS
        - `jy-project-vpc-private-subnet-2c`
            - 10.0.0.192/26
            - ECS Task, RDS
- [Internet Gateway]
    - `jy-project-vpc-igw`
- [NAT Gateway]
    - `jy-project-vpc-nat-gw`
        - 배치: `jy-project-vpc-public-subnet-2a`
        - EIP 할당
- [Routing Table]
    - `jy-project-vpc-public-rt`
        - Destination: 0.0.0.0/0 -> `jy-project-vpc-igw`
        - Subnet associations: `jy-project-vpc-public-subnet-2a`, `jy-project-vpc-public-subnet-2c`
    - `jy-project-vpc-private-rt`
        - Destination: 0.0.0.0/0 -> `jy-project-vpc-nat-gw`
        - Subnet associations: `jy-project-vpc-private-subnet-2a`, `jy-project-vpc-private-subnet-2c`
- [Security Group]
    - `jy-project-alb-sg`
        - ALB SG: 80 인바운드 전체 허용, ECS Task SG로 아웃바운드
        - Inbound rules
            - Type HTTP(80), Source Anywhere-IPv4(0.0.0.0/0)
        - Outbound rules
            - 기본값
    - `jy-project-ecs-task-sg`
        - ECS Task SG: ALB SG로부터 Gateway 포트 인바운드, 같은 SG 내 서비스 간 인바운드(Service Connect), RDS SG·인터넷(NAT)으로 아웃바운드
        - Inbound rules
            - Type Custom TCP, Port 8000, Source Custom → jy-project-alb-sg 선택
            - 생성 완료 후, 이 SG를 다시 열어서Inbound rules 1개 추가
                - Type Custom TCP, Port 8000, Source Custom → 자기 자신(jy-project-ecs-task-sg) 선택 후 저장
                - Service Connect로 서비스끼리 통신할 때 필요한 규칙
        - Outbound rules
            - 기본값
    - `jy-project-rds-sg`
        - RDS SG: ECS Task SG로부터만 5432 인바운드
        - Inbound rules
            - PostgreSQL(자동으로 포트 5432 지정), Source Custom → `jy-project-ecs-task-sg`
        - Outbound rules
            - 기본값

#### 3.2 ALB

- [Target Group]
    - `jy-project-gateway-tg`
        - Target type: **IP addresses** (awsvpc 모드라 반드시)
        - Protocol: HTTP, Port: 8000 (Gateway 컨테이너 포트와 동일)
        - VPC: `jy-project-vpc`
        - Health checks: Health check path`/health`
- [ALB]
    - `jy-project-alb`
        - Scheme: Internet-facing
        - IP address type: IPv4
        - VPC: `jy-project-vpc`
        - Mappings: **Public** 서브넷 2개(2a, 2c)
        - Security groups: `jy-project-alb-sg`
        - Listeners and routing: Protocol HTTP, Port 80 → `jy-project-gateway-tg`
        - Reservation/Inventory/Payment/Notification은 ALB 미연결 — Service Connect 내부 통신만

#### 3.3 RDS (Inventory 전용)

- DB Subnet Group
    - `jy-project-rds-subnet-group`
        - VPC: `jy-project-vpc`
        - Add subnets: AZ `ap-northeast-2a`, `ap-northeast-2c` Private 서브넷
- [RDS]
    - DB instance identifier: **`jy-project-db`**
    - Availabil`ity and durability: Single DB instance`
    - Credentials managemen`t: Managed in AWS Secrets Manager`
    - Instance configurati`on: db.t3.micro`
    - Storage
        - gp3
        - 20GB
        - Storage autoscaling 끔
    - Connectivity
        - VPC: `jy-project-vpc`
        - DB subnet group: `jy-project-rds-subnet-group`
        - Public access: No
        - VPC security group: `jy-project-rds-sg`
- 자격증명: RDS 콘솔의 Secrets Manager 관리형 옵션 사용

#### 3.4 ECS / Service Connect

- [Name Space]
    - Cloud Map 네임스페이스
    - Namespace name: `jy-project`
    - Namespace type: API 호출
    - 서비스별 discoveryName: `reservation`, `inventory`, `payment`, `notification` (Gateway는 서버 역할 없어 discoveryName 없음 — 3.4.1 참고)
- [Fargate Cluster]
    - `jy-project-cluster`
        - Infrastructure: AWS Fargate (serverless)
        - **Namespace :**`jy-project`

#### 3.4.1 ECS 서비스 생성 시 주의사항 (Phase 1 배포 기준)

- [서비스 이름]
    - 콘솔 기본 제안값(`jy-project-<service>-service-<랜덤문자열>`) 그대로 두지 않음 — CI 워크플로우가 찾는 이름과 불일치해 `Deploy to ECS` 실패
    - ECS 서비스는 생성 후 이름 변경 불가, 삭제 후 재생성 필요
    - 확정 규칙: `jy-project-<service>-service` (랜덤 접미사 없이 직접 입력)
- [네트워크]
    - 서브넷: private(2a, 2c) 2개만 선택
    - 퍼블릭 IP 자동 할당: 비활성화 (private 서브넷은 IGW 라우트 없어 실효 없음, NAT Gateway 아웃바운드 구조와 불일치)
- [Service Connect 모드]
    - 기준: 다른 서비스가 Service Connect로 호출해야 하는가

    | 서비스 | 모드 | 이유 |
    |---|---|---|
    | Gateway | 클라이언트 모드 | Reservation만 호출, ALB가 태스크 IP 직접 타겟팅이라 Gateway를 부르는 서비스 없음 |
    | Reservation | 클라이언트-서버 모드 | Gateway가 호출(서버) + Inventory/Payment/Notification 호출(클라이언트) |
    | Inventory / Payment / Notification | 클라이언트-서버 모드 | Reservation이 호출 |

    - 클라이언트 모드도 Service Connect 자체(네임스페이스 `jy-project`)는 켜져 있어야 함 — 짧은 이름은 Envoy 사이드카 기반 특수 주소라 호출하는 쪽도 꺼져 있으면 통신 불가
    - 클라이언트-서버 모드: 포트 매핑(`inventory-8000` 등) 추가 + Discovery name을 서비스명 그대로(`inventory` 등) 직접 지정
- [Service Connect DNS]
    - discovery name이 `inventory`여도 실제 resolve 가능한 주소는 `inventory.jy-project`(네임스페이스 접미사 포함)
    - taskdef 환경변수(`INVENTORY_URL` 등)는 FQDN으로 설정 — 짧은 이름은 `Name or service not known`
    - 로컬 `docker-compose.local.yml`은 도커 임베디드 DNS라 영향 없음
- [Execution Role 권한]
    - `AmazonECSTaskExecutionRolePolicy`는 `logs:CreateLogGroup` 미포함
    - `awslogs-create-group: true` 사용 시 이 권한 없으면 `ResourceInitializationError`로 태스크 기동 자체 실패
    - `ecs-demo-execution-role`에 `logs:CreateLogGroup`(리소스: `arn:aws:logs:ap-northeast-2:263232886346:log-group:/ecs/jy-project-*`) 인라인 정책 추가로 해결

#### 3.5 ECR

- 리포지토리 5개(서비스당 1개)
    - `jy-project/gateway`
    - `jy-project/reservation`
    - `jy-project/inventory`
    - `jy-project/payment`
    - `jy-project/notification`

### 3.6 IAM Role

- Role name: `ecs-demo-execution-role`
    - Trusted entity type: `AWS service`
    - Use case: `Elastic Container Service` → **`Elastic Container Service Task`**
    - Permissions policies: `AmazonECSTaskExecutionRolePolicy`
        - ECR pull + CloudWatch Logs 쓰기 권한 포함된 AWS 관리형 정책
    - 생성 후 Secrets Manager 읽기 권한을 추가 (인라인 정책명: `secrets-read`)
        - RDS 자격증명을 `DATABASE_URL`로 주입하려면 필요
            
            ```jsx
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": "secretsmanager:GetSecretValue",
                  "Resource": "arn:aws:secretsmanager:ap-northeast-2:263232886346:secret:*"
                }
              ]
            }
            ```
            
    - `logs:CreateLogGroup` 인라인 정책 추가 (정책명: `allow-cloudwatch-log-group-creation`)
        - `AmazonECSTaskExecutionRolePolicy`가 `CreateLogStream`/`PutLogEvents`만 포함하고 `CreateLogGroup`은 빠져 있어서, taskdef의 `awslogs-create-group: true`가 동작하려면 별도 추가 필요 (3.4.1 참고)
            
            ```jsx
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": "logs:CreateLogGroup",
                  "Resource": "arn:aws:logs:ap-northeast-2:263232886346:log-group:/ecs/jy-project-*"
                }
              ]
            }
            ```
            
    - SSM Parameter Store 읽기 + KMS 복호화 권한 추가 (인라인 정책명: `param_read`, Phase 2)
        - Datadog Agent/FireLens 컨테이너가 `/jy-project/*` 아래 SSM 파라미터(Datadog API Key 등, 3.7 참고)를 Fargate 기동 시점에 직접 조회하려면 필요. `kms:Decrypt`가 없으면 `datadog-api-key`(SecureString) 조회 시 `ResourceInitializationError`로 태스크 기동 실패
            
            ```jsx
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": "ssm:GetParameters",
                  "Resource": "arn:aws:ssm:ap-northeast-2:263232886346:parameter/jy-project/*"
                },
                {
                  "Effect": "Allow",
                  "Action": "kms:Decrypt",
                  "Resource": "arn:aws:kms:ap-northeast-2:263232886346:key/cc5ac505-a5e1-49ba-b1c3-1093adc44a74"
                }
              ]
            }
            ```
            
- Role name: ecs-demo-task-role
    - Trusted entity type: `AWS service`
    - Use case: `Elastic Container Service` → **`Elastic Container Service Task`**
    - Permissions policies:
    - 나중에 Datadog Agent 붙일 때 필요한 권한을 여기에 추가 — Agent가 ECS/CloudWatch API를 폴링하며 `AccessDenied`를 내는 경우에만 최소 권한으로 추가 예정(현재 미정)

### 3.7 SSM Parameter Store

- Phase 2(Datadog 사이드카) 작업을 위해 taskdef 5개 파일에 흩어져 있던 ARN과 Datadog 설정값을 `/jy-project/*` 네임스페이스로 중앙 관리
- `.env` 파일 방식은 검토 후 채택하지 않음 — CI 러너가 gitignore된 로컬 파일을 읽을 수 없어 결국 GitHub Actions Variables 같은 별도 CI 전용 저장소가 필요해지고, 로컬/CI 두 저장소를 사람이 수동 동기화해야 하는 drift 위험이 있어 SSM 단일 소스로 통일
- 소비 방식 두 갈래: taskdef 최상위 필드·FireLens 옵션처럼 등록 시점에 값이 확정돼야 하는 항목은 CI가 조회 후 치환, 컨테이너 `secrets.valueFrom`으로 넣을 수 있는 항목은 ECS가 Fargate 기동 시점에 직접 조회(3.6의 `param_read`, 4.4의 OIDC 역할 권한이 각각 이 두 갈래에 대응)

| Path | Type | 값 | 용도 |
|---|---|---|---|
| `/jy-project/ecs-execution-role-arn` | String | `arn:aws:iam::263232886346:role/ecs-demo-execution-role` | taskdef 최상위 `executionRoleArn`, CI가 등록 전 치환 |
| `/jy-project/ecs-task-role-arn` | String | `arn:aws:iam::263232886346:role/ecs-demo-task-role` | taskdef 최상위 `taskRoleArn`, CI가 등록 전 치환 |
| `/jy-project/rds-secret-arn` | String | `arn:aws:secretsmanager:ap-northeast-2:263232886346:secret:rds!db-18dce600-05b3-4a42-9284-3eeb5b617745-NHTBAD` | inventory taskdef의 RDS `secrets.valueFrom` 접두사, CI가 등록 전 치환. 값 자체는 3.3의 RDS 관리형 Secrets Manager 시크릿 ARN을 그대로 가리키는 포인터 |
| `/jy-project/dd-site` | String | `datadoghq.com` | Datadog Agent 컨테이너는 `secrets.valueFrom`으로 런타임에 직접 조회, FireLens `Host` 옵션 문자열은 CI가 치환 |
| `/jy-project/datadog-api-key` | SecureString(KMS `alias/aws/ssm`, key ARN `arn:aws:kms:ap-northeast-2:263232886346:key/cc5ac505-a5e1-49ba-b1c3-1093adc44a74`) | Datadog API Key 원문 | Datadog Agent/FireLens 컨테이너가 `secrets`/`secretOptions`의 `valueFrom`으로 런타임에 직접 조회. CI(4.4의 OIDC 역할)는 `kms:Decrypt` 권한이 없어 이 값을 복호화할 수 없음 — 원문 API Key가 CI 파이프라인을 거치지 않는 구조 |

---

## 4. CI/CD

#### 4.1 저장소 구조

- 모노레포, 서비스별 디렉터리 분리

```
/gateway, /reservation, /inventory, /payment, /notification
  각 디렉터리: Dockerfile, main.py, requirements.txt
/.github/workflows/build-and-deploy.yml
```

#### 4.2 파이프라인 범위

- 최초 인프라(클러스터·서비스·VPC·RDS)는 콘솔에서 수동 생성. Task Definition은 최초 push부터 CI가 자동 등록(수동 생성 아님). 이후 코드 변경 시 이미지 빌드부터 서비스 배포까지는 CI가 자동화

#### 4.3 워크플로우 단계

1. 코드 체크아웃
2. AWS 인증 (OIDC로 IAM Role 위임, Access Key 미사용)
3. ECR 로그인
4. 서비스별 Docker 이미지 빌드 (matrix 전략, 워크플로우 파일 1개로 5개 서비스 처리)
5. ECR 푸시 — 커밋 SHA 태그만 사용 (`latest` 태그 미사용)
6. Task Definition JSON 템플릿(레포에 커밋된 5개 파일)의 이미지 URI를 새 이미지로 교체 (`amazon-ecs-render-task-definition` 액션)
7. `register-task-definition`으로 새 리비전 등록, `update-service`로 서비스 갱신 (`amazon-ecs-deploy-task-definition` 액션)

트리거: `main` 브랜치 push

#### 4.4 IAM Role 권한 (OIDC)

- Role: `arn:aws:iam::263232886346:role/github-actions-ecs-deploy`
    - GitHub Actions(OIDC)가 위임받아 사용하는 역할. ECS-on-Fargate-Observability-with-Datadog 레포의 CI/CD 파이프라인에서 ECR 이미지 풀/푸시, ECS Task Definition 등록, 서비스 배포에 사용됨
    - Inline Policy
        
        ```jsx
        {
        	"Version": "2012-10-17",
        	"Statement": [
        		{
        			"Effect": "Allow",
        			"Action": "ecr:GetAuthorizationToken",
        			"Resource": "*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ecr:BatchCheckLayerAvailability",
        				"ecr:GetDownloadUrlForLayer",
        				"ecr:BatchGetImage",
        				"ecr:PutImage",
        				"ecr:InitiateLayerUpload",
        				"ecr:UploadLayerPart",
        				"ecr:CompleteLayerUpload"
        			],
        			"Resource": "arn:aws:ecr:ap-northeast-2:263232886346:repository/*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ecs:RegisterTaskDefinition",
        				"ecs:DescribeTaskDefinition"
        			],
        			"Resource": "*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ecs:UpdateService",
        				"ecs:DescribeServices"
        			],
        			"Resource": "arn:aws:ecs:ap-northeast-2:263232886346:service/*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": "iam:PassRole",
        			"Resource": [
        				"arn:aws:iam::263232886346:role/ecs-demo-task-role",
        				"arn:aws:iam::263232886346:role/ecs-demo-execution-role"
        			],
        			"Condition": {
        				"StringEquals": {
        					"iam:PassedToService": "ecs-tasks.amazonaws.com"
        				}
        			}
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ssm:GetParameters",
        				"ssm:GetParametersByPath"
        			],
        			"Resource": "arn:aws:ssm:ap-northeast-2:263232886346:parameter/jy-project/*"
        		}
        	]
        }
        ```
        
    - Phase 2에서 taskdef 등록 전 SSM 값 치환(3.7 참고)을 위해 `ssm:GetParameters`/`ssm:GetParametersByPath` 추가. **의도적으로 `kms:Decrypt`는 부여하지 않음** — `datadog-api-key`(SecureString)를 이 역할이 복호화하지 못하게 막아, 원문 API Key가 CI 파이프라인 경로를 절대 거치지 않도록 하는 방어선

---

## 5. 관측성 통합 체크리스트

- 5개 서비스 전부 동일 적용 (Notification 포함 — 하나라도 빠지면 5.3 Trace-to-Log 체인이 끊김).

#### 5.1 Datadog Agent 사이드카 (인프라 메트릭)

- 이미지 `datadog/agent:latest`
- 환경변수: `DD_API_KEY`(Secrets Manager), `ECS_FARGATE=true`, `DD_SITE`

#### 5.2 FireLens 사이드카 (로그)

- `firelensConfiguration.type: fluentbit`
- 앱 컨테이너 `logConfiguration.logDriver: awsfirelens`
- Datadog 목적지 옵션에 `dd_service`를 서비스명으로 지정

#### 5.3 dd-trace (APM)

- `ddtrace` 라이브러리 설치, 기동 명령 `ddtrace-run uvicorn main:app`
- 환경변수: `DD_SERVICE`, `DD_ENV=demo`, `DD_AGENT_HOST=localhost`
- 서비스 간 호출(httpx)의 트레이스 컨텍스트 자동 전파 여부 사전 확인 — Reservation→Inventory/Payment→Notification 체인이 트레이스 하나로 엮여야 함

#### 5.4 네트워크 (보류)

- 데모 진행하며 검증 후 반영 예정 (CNM / VPC Flow Logs / Service Connect 메트릭 중 확정).

#### 5.5 대시보드 및 알림

- 대시보드 위젯: 서비스별 CPU/메모리(5.1), 에러 로그 건수(5.2), 트레이스 에러율·레이턴시(5.3)
- Monitor 예시: Payment 에러율 임계치 초과, Reservation 종단 레이턴시 임계치 초과 — 5.3 데모에서 장애를 최초로 감지하는 트리거

---

## 6. 리소스 스펙

- Fargate Task CPU/메모리: 512 / 1024 MB (앱 128/256, Datadog Agent 256/512, FireLens 128/256)
- Desired count: 서비스당 1
- RDS: db.t3.micro, 20GB gp3, Single-AZ
- NAT Gateway: 1개
- ALB: 1개
- 리전: ap-northeast-2 (서울)

---

## 7. 장애 주입 메커니즘

- 라이브 데모 특성상 랜덤 장애가 아니라 즉시 토글 가능한 방식 채택. Task 재배포 방식(30초~1분 소요)은 발표 흐름을 끊으므로 배제.

#### 7.1 Payment — 주 장애 주입 지점

- `POST /admin/chaos` 엔드포인트 (in-memory 플래그, 재기동 불필요)

```json
{ "mode": "latency" | "error" | "off", "delay_ms": 5000, "error_rate": 1.0 }
```

- `latency`: `/charge` 응답에 지정 지연 강제 추가 → Reservation 병렬 호출 중 하나가 느려지며 전체 예약 응답 지연/타임아웃
- `error`: 지정 비율로 500 강제 반환 → Reservation의 보상 트랜잭션(Inventory `/release`) 흐름까지 시연 가능
- `off`: 정상 복귀

#### 7.2 Inventory — 보조 시나리오

기존 좌석 잠금 로직(409 Conflict)을 그대로 활용. 동일 좌석에 동시 예약 요청 2개를 보내면 자연 재현. 인프라 장애가 아닌 비즈니스 로직 충돌 시연용.

#### 7.3 배경 트래픽 생성

장애 주입 전 정상 베이스라인이 그래프에 깔려 있어야 이상치가 드러남. 부하 생성 스크립트(반복 `POST /reservations` 호출, 또는 `hey`/`k6`)로 데모 시작 몇 분 전부터 초당 일정 트래픽 유지.

#### 7.4 주의사항

`/admin/chaos`는 인증 없이 열어둔 데모 전용 엔드포인트. 실제 프로덕션 패턴이 아님.

---

## 8. 정리

- 데모 종료 후 NAT Gateway와 ALB는 상시 과금 리소스. 데모 종료 후 ECS 서비스/클러스터, RDS, ALB, NAT Gateway, VPC 순으로 리소스 정리 필요